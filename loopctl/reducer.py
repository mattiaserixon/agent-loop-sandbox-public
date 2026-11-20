"""The idempotent reducer: events -> state.json (phase2-design §3.2 transitions, invariants I1, I4, I6, I8, I10).

`reduce(state, events)` is pure. It applies events in id order (ULIDs sort by time) and:

- **I4** applies each event at most once: every event id it has seen is kept in the unit's `applied` list (or the
  state's `applied` list for events naming no unit, kept 7 days by the time in the id), and a seen id is skipped.
  A rejected event is remembered too, so a replay can't apply it later.
- **I10** accepts a controller event only at the current controller epoch; `controller.lease` events are judged by
  the lease rules (take at epoch + 1, renew by the holder at its epoch).
- **I8** accepts a worker event for a unit only with the unit's current lease token (its sha256 matches the lease,
  decision record 0029) and a writer of the lease's kind; once a worker event carrying a claimant is applied, the
  lease records it (`claimant_sha256`) and later worker events must carry the same one (the first-call claim,
  `loopctl/claim.py`).
- **I1** grants a lease only on a unit holding none; **I6** grants none in `needs_owner`, and opening a question
  drops the unit's lease.
- **I7** refuses a review or fix lease, and a review, past the round cap (verdict.round_cap) unless the owner
  granted more rounds; the verdict is computed from findings (findings.classify, verdict.compute), never chosen.
- **I3** records `merge.done` as `merged` only when the merge preconditions hold (verdict.check_merge 1, 2, 3, 4,
  6) and main's tree after the merge equals the tree of T the gate passed on (`tested_tree`); a merge around them
  puts the unit in `needs_owner` with a `violation`.
- **Post-merge check** (§10.6): `main.moved` makes main's new sha unchecked (red stays red until a check passes) and
  queues a `check-<sha>` unit; `main.checked` sets `main.green`; `revert.due` queues a `revert-<sha>` unit whose
  `revert.pushed` PR is approved without review (an exact revert: `loopctl gate` checks its tree).

Rejections are returned with their reasons; they never raise.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from loopctl import budget, clock, schema
from loopctl.findings import classify
from loopctl.leases import GRANT_MOVES, GRANTABLE, MAX_ATTEMPTS, NO_LEASE_STATES, token_hash
from loopctl.verdict import MergeCheck, check_merge, compute, round_cap

GLOBAL_APPLIED_DAYS = 7
CROCKFORD = clock.CROCKFORD

# Who may write each event type. "holder" = a worker holding the unit's lease (kind given), "controller" = the
# controller at the current epoch, "workflow" = the verify workflow (App-authored commits, M0.7/M0.11).
WRITERS: dict[str, set[str]] = {
    "issue.seen": {"controller"},
    "pr.seen": {"controller"},
    "pr.risk": {"controller"},
    "pr.head": {"controller"},
    "approval.recorded": {"controller"},
    "triage.done": {"worker:triage"},
    "lease.granted": {"controller"},
    "lease.bound": {"controller"},
    "lease.renewed": {"controller", "holder"},
    "lease.released": {"controller", "holder"},
    "lease.expired": {"controller"},
    "fix.pushed": {"worker:fix"},
    "fix.unchanged": {"worker:fix"},
    "review.submitted": {"worker:review"},
    "finding.fixed": {"worker:fix"},
    "finding.reopened": {"worker:review"},
    "finding.waived": {"controller"},
    "gate.passed": {"worker:gate"},
    "gate.failed": {"worker:gate"},
    "merge.requested": {"worker:gate", "controller"},
    "merge.done": {"controller", "workflow"},
    "merge.audited": {"controller"},
    "question.opened": {"controller"},
    "question.answered": {"controller"},
    "unit.closed": {"controller"},
    "tick.done": {"controller"},
    "stop.seen": {"controller"},
    "event.restamped": {"controller"},
    "cost.observed": {"controller"},
    "limit.observed": {"controller"},
    "controller.lease": {"controller"},
    "command.received": {"controller"},
    "command.applied": {"controller"},
    "command.rejected": {"controller"},
    "controller.alert": {"controller"},
    "reconcile.due": {"controller"},
    "reconcile.reported": {"worker:reconcile"},
    "main.moved": {"controller"},
    "main.checked": {"worker:check"},
    "revert.due": {"controller"},
    "revert.pushed": {"worker:revert"},
}
CREATES_UNIT = {"issue.seen", "pr.seen", "reconcile.due", "revert.due", "main.moved"}
ALERT_OPTIONS = [{"id": "done", "label": "Done", "effect": "clears this item", "widens": False},
                 {"id": "wait", "label": "Later", "effect": "the item stays until it is done", "widens": False}]
ALERT_DUE_MIN = 24 * 60
GATE_RETRIES = 2


@dataclass
class Outcome:
    state: dict[str, Any]
    applied: list[str] = field(default_factory=list)  # ids that changed (or were accepted into) the state
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (id, reason)
    notes: list[dict[str, Any]] = field(default_factory=list)  # note-routed findings: {path, summary, pr, round}
    issues: list[dict[str, Any]] = field(default_factory=list)  # issue-routed findings (filed by the review worker)


class Reject(Exception):
    pass


@dataclass
class Policy:
    """Inputs the reducer needs besides events: the effective config (default: the engine defaults) and the
    triaged -> queued eligibility (I9: excluded labels and paths, checked by the controller from its snapshot)."""

    cfg: Any = None
    eligible: Callable[[dict[str, Any]], bool] = lambda unit: True
    on_event: Callable[[dict[str, Any], dict[str, Any]], None] | None = None  # (state, event) for other modules

    def __post_init__(self) -> None:
        if self.cfg is None:
            from loopctl.config import resolve

            self.cfg = resolve()


def new_unit(key: str, kind: str, state: str = "new") -> dict[str, Any]:
    return {"key": key, "kind": kind, "state": state, "lease": None, "attempt": 0, "round": 0, "head": None,
            "pr": None, "tier": None, "critical": False, "applied": [], "prior_state": None, "priority": None,
            "verdict": None, "findings": {}, "changed": [], "gate_files": False, "approved_head": None,
            "approvals": [], "extra_rounds": 0, "violation": None, "gate": None, "stalled": None,
            "granted_in": None, "handoff": None, "revert": None}  # fmt: skip


def ulid_ms(ulid: str) -> int:
    value = 0
    for ch in ulid[:10]:
        value = value * 32 + CROCKFORD.index(ch)
    return value


def reduce(state: dict[str, Any], events: Iterable[dict[str, Any]], policy: Policy | None = None) -> Outcome:
    policy = policy or Policy()
    out = Outcome(copy.deepcopy(state))
    s = out.state
    for k, v in (("spend", []), ("sessions", {}), ("cost_samples", {}), ("signals", {})):
        s.setdefault(k, copy.deepcopy(v))
    s["main"].setdefault("last_green", None)
    for key, unit in s["units"].items():  # units stored before a field existed get its default
        for k, v in new_unit(key, unit["kind"]).items():
            unit.setdefault(k, v)
    seen_global = set(s["applied"])
    batch = sorted(events, key=lambda e: str(e.get("id", "")))
    restamps = _restamps(batch)
    for event in batch:
        eid = str(event.get("id", ""))
        if eid in restamps:  # T-TIME: applied with the controller's time (the event file keeps its own)
            event = {**event, "at": restamps[eid]}
        unit_key = event.get("unit") if isinstance(event.get("unit"), str) else None
        unit = s["units"].get(unit_key) if unit_key else None
        if eid in seen_global or (unit is not None and eid in unit["applied"]):
            continue  # I4: applied (or rejected) before
        try:
            if "invalid" in event:
                raise Reject(event["invalid"])
            try:
                schema.validate_event(event)
            except schema.SchemaError as exc:
                raise Reject(str(exc)) from exc
            _authorise(s, event)
            _apply(s, event, policy, out)
            _record_claim(s, event)
            out.applied.append(eid)
        except Reject as exc:
            out.rejected.append((eid, str(exc)))
        # Remember the id either way: in the unit's list if the unit exists now, else globally.
        unit = s["units"].get(unit_key) if unit_key else None
        if unit is not None:
            if eid not in unit["applied"]:
                unit["applied"].append(eid)
        elif eid and eid not in seen_global:
            s["applied"].append(eid)
            seen_global.add(eid)
    _prune_global(s)
    return out


def _restamps(batch: list[dict[str, Any]]) -> dict[str, str]:
    """event id -> the time to apply it with, from the batch's `event.restamped` events (T-TIME, decision record
    0013). Collected before anything is applied, so a re-stamped event takes its corrected time whatever order the
    ids sort in (a slow writer's clock sorts its event before the controller's re-stamp of it). The re-stamp event
    itself is authorised like any controller event when its turn comes."""
    out: dict[str, str] = {}
    for e in batch:
        p = e.get("payload")
        if e.get("type") == "event.restamped" and e.get("writer") == "controller" and isinstance(p, dict) \
                and isinstance(p.get("event"), str) and isinstance(e.get("at"), str):  # fmt: skip
            out[p["event"]] = e["at"]
    return out


def _prune_global(s: dict[str, Any]) -> None:
    valid = [i for i in s["applied"] if len(i) == 26 and all(c in CROCKFORD for c in i)]
    if not valid:
        s["applied"] = []
        return
    newest = max(ulid_ms(i) for i in valid)
    horizon = newest - GLOBAL_APPLIED_DAYS * 86_400_000
    s["applied"] = sorted(i for i in valid if ulid_ms(i) >= horizon)


def _record_claim(s: dict[str, Any], e: dict[str, Any]) -> None:
    """The first applied worker event carrying a claimant binds the lease to that session (0029, claim.py)."""
    who = e.get("claimant_sha256")
    unit = s["units"].get(e["unit"]) if who and e["writer"].startswith("worker:") and e["unit"] else None
    lease = unit["lease"] if unit else None
    if lease and not lease.get("claimant_sha256") and token_hash(e.get("lease_token") or "") == \
            lease["lease_token_sha256"]:  # fmt: skip
        lease["claimant_sha256"] = who


# ------------------------------------------------------------------------------------------------------ authority
def _authorise(s: dict[str, Any], e: dict[str, Any]) -> None:
    allowed = WRITERS[e["type"]]
    writer = e["writer"]
    if writer == "controller":
        if "controller" not in allowed:
            raise Reject(f"{e['type']} is not written by the controller")
        if e["type"] == "controller.lease":
            return  # judged in _controller_lease
        if e["type"] == "controller.alert":
            return  # any controller session may add a needs-you item: it can only ask the owner to look
        lease = s["controller"]
        if lease is None or e["epoch"] != lease["epoch"]:
            current = lease["epoch"] if lease else None
            raise Reject(f"controller event at epoch {e['epoch']}, current epoch is {current}")  # I10
        return
    if writer.startswith("worker:"):
        if writer not in allowed and "holder" not in allowed:
            raise Reject(f"{e['type']} can't be written by {writer}")
        unit = s["units"].get(e["unit"]) if e["unit"] else None
        lease = unit["lease"] if unit else None
        if lease is None:
            raise Reject(f"{writer} wrote {e['type']} for {e['unit']}, which holds no lease")  # I8
        if writer != f"worker:{lease['kind']}":
            raise Reject(f"{writer} doesn't hold {e['unit']}'s {lease['kind']} lease")
        token = e.get("lease_token")
        if not token or token_hash(token) != lease["lease_token_sha256"]:
            raise Reject(f"lease token doesn't match {e['unit']}'s current lease")  # I8, decision record 0029
        claimed = lease.get("claimant_sha256")
        if claimed and e.get("claimant_sha256") != claimed:
            raise Reject(f"{e['unit']}'s lease was claimed by another session (a duplicate start)")  # 0029 claim
        return
    if writer not in allowed:
        raise Reject(f"{e['type']} can't be written by {writer}")


# ------------------------------------------------------------------------------------------------------ transitions
def _need_unit(s: dict[str, Any], e: dict[str, Any]) -> dict[str, Any]:
    unit = s["units"].get(e["unit"]) if e["unit"] else None
    if unit is None:
        raise Reject(f"{e['type']} names no known unit ({e['unit']})")
    return unit


def _need_state(unit: dict[str, Any], *states: str) -> None:
    if unit["state"] not in states:
        raise Reject(f"{unit['key']} is {unit['state']}, not {' or '.join(states)}")


def _apply(s: dict[str, Any], e: dict[str, Any], policy: Policy, out: Outcome) -> None:
    t, p = e["type"], e["payload"]
    if t == "controller.lease":
        return _controller_lease(s, e)
    if t == "tick.done":
        s["last_tick_at"] = e["at"]
        s["config_hash"] = p["config_hash"]
        return _hook(s, e, policy)
    if t == "event.restamped":  # used by reduce() before the batch is applied (_restamps)
        return
    if t == "stop.seen":  # the STOP lines the tick read; the next tick's restoration check compares against them
        s["stop_seen"] = {"lines": list(p["lines"]), "at": e["at"]}
        return
    if t == "controller.alert":
        qid = f"q-{e['id']}"
        if qid not in s["questions"]:
            s["questions"][qid] = {"unit": None, "kind": "controller", "options": copy.deepcopy(ALERT_OPTIONS),
                                   "default_option": "done", "due_at": later(e["at"], ALERT_DUE_MIN),
                                   "opened_at": e["at"], "detail": p["problem"]}  # fmt: skip
        return
    if t in CREATES_UNIT:
        key = {"issue.seen": lambda: f"issue-{p['issue']}", "pr.seen": lambda: f"pr-{p['pr']}",
               "reconcile.due": lambda: f"reconcile-{p['day']}", "revert.due": lambda: f"revert-{p['red'][:12]}",
               "main.moved": lambda: f"check-{p['sha'][:12]}"}[t]()
        if e["unit"] != key:
            raise Reject(f"{t} for {key} names unit {e['unit']}")
        if t == "main.moved":
            return _main_moved(s, p["sha"], key)
        if key in s["units"]:
            return  # seen before: nothing changes
        if t == "revert.due":
            unit = new_unit(key, "revert", "queued")
            unit["revert"] = {"red": p["red"], "good": p["good"]}
            s["units"][key] = unit
            return
        if t == "issue.seen":
            s["units"][key] = new_unit(key, "fix")
        elif t == "reconcile.due":
            s["units"][key] = new_unit(key, "reconcile", "queued")
        else:
            unit = new_unit(key, "review", "in_review")
            unit.update(pr=p["pr"], head=p["head"])
            s["units"][key] = unit
        return
    if t in ("cost.observed", "limit.observed"):
        budget.on_event(s, e)
        return _hook(s, e, policy)
    if e["unit"] is None:
        if t == "question.opened" or t == "question.answered":
            return _question(s, e, None)
        return _hook(s, e, policy)  # commands: other modules
    unit = _need_unit(s, e)
    before = unit["state"]
    _apply_unit(s, e, policy, out, unit)
    unit = s["units"][unit["key"]]
    if unit["state"] != before:
        unit["stalled"] = None  # progress
    if t == "lease.granted":
        unit["granted_in"] = unit["state"]
    elif t == "lease.released" and unit["state"] == unit["granted_in"] and e["payload"]["reason"] == "finished":
        unit["stalled"] = unit["state"]  # the worker ended without progress: not started again for this step (§4.3)


def _apply_unit(s: dict[str, Any], e: dict[str, Any], policy: Policy, out: Outcome, unit: dict[str, Any]) -> None:
    t, p = e["type"], e["payload"]

    if t == "lease.granted":
        kind = p["kind"]
        if unit["lease"] is not None:
            raise Reject(f"{unit['key']} already holds a {unit['lease']['kind']} lease")  # I1
        if unit["state"] in NO_LEASE_STATES or unit["state"] not in GRANTABLE[kind]:
            raise Reject(f"a {kind} lease can't be granted on {unit['key']} in {unit['state']}")  # I6
        if p["attempt"] != unit["attempt"] + 1 or unit["attempt"] >= MAX_ATTEMPTS:
            raise Reject(f"grant attempt {p['attempt']} on {unit['key']} after {unit['attempt']} attempts")
        cap = round_cap(unit, policy.cfg)
        if (kind == "review" or (kind == "fix" and unit["state"] == "changes")) and unit["round"] >= cap:
            raise Reject(f"{unit['key']} has had {unit['round']} review rounds, the cap is {cap}")  # I7
        unit["lease"] = {"kind": kind, "holder_session": p["holder_session"], "granted_at": e["at"],
                         "expires_at": p["expires_at"], "attempt": p["attempt"],
                         "lease_token_sha256": p["lease_token_sha256"]}  # fmt: skip
        unit["state"] = GRANT_MOVES.get((kind, unit["state"]), unit["state"])
    elif t == "lease.bound":
        lease = _need_lease(unit)
        if lease["holder_session"] not in (None, p["session"]):
            raise Reject(f"{unit['key']}'s lease is already bound to {lease['holder_session']}")
        lease["holder_session"] = p["session"]
        budget.on_event(s, e)
    elif t == "lease.renewed":
        _need_lease(unit)["expires_at"] = p["expires_at"]
    elif t == "lease.released":
        _need_lease(unit)
        unit["lease"] = None
        if "outcome" in p:
            unit["handoff"] = {"outcome": p["outcome"], "note": p.get("note", ""), "at": e["at"]}
    elif t == "lease.expired":
        lease = _need_lease(unit)
        if p["attempt"] != lease["attempt"]:
            raise Reject(f"expiry names attempt {p['attempt']}, the lease is attempt {lease['attempt']}")
        unit["lease"] = None
        unit["attempt"] += 1
    elif t == "triage.done":
        _need_state(unit, "new")
        unit["verdict"], unit["priority"] = p["verdict"], p["priority"]
        if p["verdict"] in ("rejected", "duplicate"):
            unit["state"] = "closed"
        elif p["verdict"] == "accepted" and policy.eligible(unit):
            unit["state"] = "queued"
        else:
            unit["state"] = "triaged"
        unit["attempt"] = 0
    elif t == "fix.pushed":
        _need_state(unit, "building")
        if unit["pr"] is not None and unit["pr"] != p["pr"]:
            raise Reject(f"{unit['key']} is PR {unit['pr']}, not {p['pr']}")
        unit.update(state="in_review", pr=p["pr"], head=p["head"], attempt=0, approved_head=None, gate=None)
    elif t == "fix.unchanged":  # nothing to change: an approved head goes to the gate again, another to review
        _need_state(unit, "building")
        if unit["pr"] is None or p["head"] != unit["head"]:
            raise Reject(f"{str(p['head'])[:12]} is not {unit['key']}'s PR head: push a fix instead")
        approved = unit.get("approved_head") == unit["head"]
        unit.update(state="approved" if approved else "in_review", attempt=0, gate=None)
    elif t == "pr.head":
        if unit["pr"] != p["pr"]:
            raise Reject(f"{unit['key']} is PR {unit['pr']}, not {p['pr']}")
        if p["head"] == unit["head"]:
            return
        idle_build = unit["state"] == "building" and unit.get("lease") is None  # a fix that stalled: no push is coming
        if unit["state"] not in ("in_review", "changes", "approved", "gating") and not idle_build:
            raise Reject(f"{unit['key']} is {unit['state']}: its head moves only through fix.pushed")
        # New commits outside the loop: the approval and the gate result were for the old head (§9.4 (1), (4)).
        unit.update(head=p["head"], approved_head=None, gate=None, stalled=None)  # new commits are progress
        if unit["state"] in ("approved", "gating"):
            unit["state"] = "in_review"
    elif t == "pr.risk":
        if p["head"] != unit["head"]:
            raise Reject(f"risk of {p['head'][:12]}, the unit's head is {str(unit['head'])[:12]}")
        unit.update(tier=p["tier"], critical=p["critical"], gate_files=p["gate_files"], changed=list(p["changed"]))
    elif t == "approval.recorded":
        if p["head"] not in unit["approvals"]:
            unit["approvals"].append(p["head"])
            # An owner approval is progress for a unit stalled where it was missing (a critical PR gated before the
            # owner approved: the gate passed but no merge could be requested). Without this the unit was never
            # started again (found by the capacity replay, 2026-11-18: 11 critical PRs held in `gating`).
            unit["stalled"] = None
    elif t == "review.submitted":
        _need_state(unit, "in_review")
        if p["head"] != unit["head"] or p["pr"] != unit["pr"]:
            raise Reject(f"review of {p['head'][:12]} on PR {p['pr']}, the unit's head is {str(unit['head'])[:12]}")
        if p["round"] != unit["round"] + 1:
            raise Reject(f"review round {p['round']} after round {unit['round']}")
        if p["round"] > round_cap(unit, policy.cfg):
            raise Reject(f"review round {p['round']} is over the cap {round_cap(unit, policy.cfg)}")  # I7
        ids = [f["id"] for f in p["findings"]]
        if len(ids) != len(set(ids)) or set(ids) & set(unit["findings"]):
            raise Reject("finding ids must be new and unique")
        for f in unit["findings"].values():
            if f["status"] == "fixed" and f["head"] != p["head"]:
                f["status"] = "verified"  # the next round didn't reopen it
        for f in p["findings"]:
            route = classify(f, unit["changed"], policy.cfg)
            unit["findings"][f["id"]] = {"head": p["head"], "round": p["round"], "path": f["path"],
                                         "severity": f["severity"], "introduced": f["introduced"],
                                         "summary": f["summary"], "route": route, "status": "open",
                                         "fixed_commit": None}  # fmt: skip
            if route == "note":
                out.notes.append({"path": f["path"], "summary": f["summary"], "pr": p["pr"], "round": p["round"]})
            elif route == "issue":
                out.issues.append(dict(f, pr=p["pr"]))
        unit["round"] = p["round"]
        unit["state"] = compute(unit)
        unit["approved_head"] = p["head"] if unit["state"] == "approved" else None
        unit["attempt"] = 0
    elif t == "finding.fixed":
        f = _finding(unit, p["finding_id"])
        if f["route"] != "blocking" or f["status"] != "open":
            raise Reject(f"finding {p['finding_id']} is not an open blocking finding")
        f.update(status="fixed", fixed_commit=p["commit"])
    elif t == "finding.reopened":
        f = _finding(unit, p["finding_id"])
        if f["status"] not in ("fixed", "verified"):
            raise Reject(f"finding {p['finding_id']} is {f['status']}")
        f.update(status="open", fixed_commit=None)
    elif t == "finding.waived":
        f = _finding(unit, p["finding_id"])
        if f["status"] != "open":
            raise Reject(f"finding {p['finding_id']} is {f['status']}")
        f["status"] = "waived"
        if unit["state"] in ("changes", "in_review") and compute(unit) == "approved" and unit["head"] == f["head"]:
            unit.update(state="approved", approved_head=unit["head"])
    elif t in ("gate.passed", "gate.failed"):
        _need_state(unit, "gating")
        if p["result"]["head"] != unit["head"]:
            raise Reject(f"gate result for {p['result']['head'][:12]}, the unit's head is {str(unit['head'])[:12]}")
        r = p["result"]
        unit["gate"] = {k: r[k] for k in ("head", "tested", "base", "result", "gate_command_sha256", "log_sha256",
                                          "tested_tree") if k in r}
        if t == "gate.passed" and r["result"] != "pass":
            raise Reject("gate.passed carries a failing result")
        if t == "gate.failed":
            unit["state"] = "changes" if p["attributed"] else "approved"
            unit["gate"] = None
        _hook(s, e, policy)
    elif t == "merge.requested":
        _need_state(unit, "gating")
    elif t == "merge.done":  # a GitHub fact: recorded in any open state, merged only if the preconditions held
        if unit["state"] in ("merged", "closed", "new", "triaged", "queued"):
            raise Reject(f"{unit['key']} is {unit['state']}")
        if p["pr"] != unit["pr"]:
            raise Reject(f"merge of PR {p['pr']}, the unit's PR is {unit['pr']}")
        check = check_merge(unit, pr_head=unit["head"], cfg=policy.cfg, tested=p["tested"], checks=(1, 2, 3, 4, 6))
        tree = (unit.get("gate") or {}).get("tested_tree")
        if check.ok and (tree is None or tree != p.get("merged_tree")):  # I3: main's new tree = T's tree
            check = MergeCheck([(4, f"main's tree after the merge ({str(p.get('merged_tree'))[:12]}) isn't the tree "
                                    f"the gate tested ({str(tree)[:12]})")])  # fmt: skip
        if check.ok:
            unit.update(state="merged", lease=None)
        else:  # I3: GitHub merged it, but not through the preconditions: the owner decides
            if unit["state"] != "needs_owner":
                unit["prior_state"] = unit["state"]
            unit.update(state="needs_owner", lease=None,
                        violation=f"merged around the gate: {check.reason()}"[:500])
        _hook(s, e, policy)
    elif t == "question.opened" or t == "question.answered":
        _question(s, e, unit)
    elif t == "unit.closed":
        unit.update(state="closed", lease=None)
    elif t == "main.checked":
        _need_state(unit, "building")
        r = p["result"]
        if unit["kind"] != "check" or r["head"] != unit["head"] or r["tested"] != unit["head"]:
            raise Reject(f"a check of {r['head'][:12]} on {unit['key']} (main at {str(unit['head'])[:12]})")
        unit.update(state="closed", lease=None, gate={k: r[k] for k in ("head", "tested", "base", "result",
                                                                       "gate_command_sha256", "log_sha256")})  # fmt: skip
        if r["head"] == s["main"]["sha"]:
            s["main"]["green"] = r["result"] == "pass"
            if r["result"] == "pass":
                s["main"]["last_green"] = r["head"]
        _hook(s, e, policy)
    elif t == "revert.pushed":
        _need_state(unit, "building")
        if unit["kind"] != "revert":
            raise Reject(f"{unit['key']} is not a revert unit")
        # An exact revert's verdict is computed without a review (§10.6); `loopctl gate` checks that its tree is the
        # last green tree before it passes.
        unit.update(state="approved", pr=p["pr"], head=p["head"], approved_head=p["head"], attempt=0, gate=None)
    elif t == "reconcile.reported":
        _need_state(unit, "building")
        unit.update(state="closed", lease=None)
        _hook(s, e, policy)
    else:
        _hook(s, e, policy)  # findings, audits, costs: other modules


def _main_moved(s: dict[str, Any], sha: str, key: str) -> None:
    main = s["main"]
    if main["sha"] == sha:
        return
    main["sha"] = sha
    main["green"] = False if main["green"] is False else None  # red stays red until a check on main passes
    for k, u in s["units"].items():  # checks of older shas that never started are moot
        if u["kind"] == "check" and u["state"] == "queued" and not u["lease"]:
            u["state"] = "closed"
    if key not in s["units"]:
        unit = new_unit(key, "check", "queued")
        unit["head"] = sha
        s["units"][key] = unit


def _finding(unit: dict[str, Any], fid: str) -> dict[str, Any]:
    if fid not in unit["findings"]:
        raise Reject(f"{unit['key']} has no finding {fid}")
    return unit["findings"][fid]


def _need_lease(unit: dict[str, Any]) -> dict[str, Any]:
    if unit["lease"] is None:
        raise Reject(f"{unit['key']} holds no lease")
    return unit["lease"]


def _hook(s: dict[str, Any], e: dict[str, Any], policy: Policy) -> None:
    if policy.on_event:
        policy.on_event(s, e)


def _question(s: dict[str, Any], e: dict[str, Any], unit: dict[str, Any] | None) -> None:
    p = e["payload"]
    if e["type"] == "question.opened":
        qid = p["question_id"]
        if qid in s["questions"]:
            raise Reject(f"question {qid} is already open")
        s["questions"][qid] = {"unit": unit["key"] if unit else None, "kind": p["kind"], "options": p["options"],
                               "default_option": p["default_option"], "due_at": p["due_at"], "opened_at": e["at"]}  # fmt: skip
        if p.get("detail"):
            s["questions"][qid]["detail"] = p["detail"]
        if unit is not None:
            if unit["state"] in ("closed", "merged"):
                raise Reject(f"{unit['key']} is {unit['state']}")
            if unit["state"] != "needs_owner":
                unit["prior_state"] = unit["state"]
            unit.update(state="needs_owner", lease=None)  # I6: no lease, no capacity
        return
    qid = p["question_id"]
    question = s["questions"].get(qid)
    if question is None:
        raise Reject(f"no open question {qid}")
    if p["option"] not in {o["id"] for o in question["options"]}:
        raise Reject(f"{p['option']!r} is not an option of {qid}")
    if (unit["key"] if unit else None) != question["unit"]:
        raise Reject(f"question {qid} is about {question['unit']}")
    del s["questions"][qid]
    if unit is not None and question["kind"] == "cap_reached":
        if p["option"] == "one_more_round":
            unit["extra_rounds"] += 1
        elif p["option"] in ("split", "close"):
            unit.update(state="closed", prior_state=None, lease=None)
            return
    if unit is not None and not any(q["unit"] == unit["key"] for q in s["questions"].values()):
        unit["state"] = unit["prior_state"] or "queued"
        unit["prior_state"] = None
        unit["attempt"] = 0


def _controller_lease(s: dict[str, Any], e: dict[str, Any]) -> None:
    p = e["payload"]
    lease = s["controller"]
    if e["epoch"] != p["epoch"]:
        raise Reject("controller.lease envelope and payload epochs differ")
    fields = {k: p[k] for k in ("session", "environment", "epoch", "granted_at", "expires_at", "tick_task")}
    if p["reason"] == "renew":
        if lease is None or lease["session"] != p["session"] or lease["epoch"] != p["epoch"]:
            raise Reject(f"renewal by {p['session']} at epoch {p['epoch']} doesn't match the lease")  # I10
        if lease["environment"] != p["environment"]:
            raise Reject("a renewal can't change the controller's environment")
        if lease["tick_task"] and p["tick_task"] != lease["tick_task"]:
            raise Reject("a renewal can't replace the recorded tick task")
        s["controller"] = dict(lease, expires_at=p["expires_at"], tick_task=p["tick_task"])
        return
    expected = (lease["epoch"] if lease else 0) + 1
    if p["epoch"] != expected:
        raise Reject(f"take at epoch {p['epoch']}, the next epoch is {expected}")  # I10
    if lease is None and p["reason"] != "first":
        raise Reject("the first lease must say reason first")
    if lease is not None:
        if p["reason"] == "first":
            raise Reject("a lease exists: this is not a first start")
        if p["reason"] == "expired" and clock.parse(lease["expires_at"]) > clock.parse(e["at"]):
            raise Reject(f"the lease held by {lease['session']} had not expired at {e['at']}")
        s["previous_controller"] = {"session": lease["session"], "tick_task": lease["tick_task"]}
    s["controller"] = fields


def later(stamp: str, minutes: int) -> str:
    return clock.stamp_at(clock.parse(stamp) + timedelta(minutes=minutes))
