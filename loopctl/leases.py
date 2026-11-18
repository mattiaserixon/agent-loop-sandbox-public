"""Unit leases (phase2-design §3.4, decision record 0004) and the controller lease with epoch fencing (§3.5, I10).

Everything here is pure: functions take the state and the time and return events (or a decision). The events take
effect only through the reducer, inside a CAS commit (store.py), so a lease exists only if the push that granted it
won (§5.3).

A unit lease is `{kind, holder_session, granted_at, expires_at, attempt, lease_token_sha256, claimant_sha256}`. At grant
a 128-bit random token is made; only its sha256 is stored, and the token goes into the worker's brief (decision record
0029). The first worker session that uses the token claims the lease (`claim.py`); the reducer records its sha256 in
`claimant_sha256` from the first applied worker event that carries one.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from loopctl import clock
from loopctl.store import make_event

# Expiry by kind (Phase 1 p90 + margin, §3.4). Scan and build are not built (build-plan §6); they get the fix value.
EXPIRY_MIN = {"fix": 90, "review": 45, "gate": 40, "triage": 20, "scan": 90, "build": 90, "reconcile": 30,
              "check": 40, "revert": 30}
CONTROLLER_LEASE_MIN = 130  # two hourly sweeps + 10 min (§3.5)
MAX_ATTEMPTS = 3

# Which unit states a lease of each kind may be granted in (§3.2; a re-grant after expiry keeps the state).
GRANTABLE = {
    "triage": {"new"},
    "fix": {"queued", "changes", "building"},
    "review": {"in_review"},
    "gate": {"approved", "gating"},
    "scan": {"queued", "building"},
    "build": {"queued", "building"},
    "reconcile": {"queued", "building"},
    "check": {"queued", "building"},  # the post-merge check of main (§10.6)
    "revert": {"queued", "building"},  # opens the revert PR when main is red (§10.6)
}
# The state a grant moves the unit to (absent: unchanged).
GRANT_MOVES = {("fix", "queued"): "building", ("fix", "changes"): "building", ("gate", "approved"): "gating",
               ("scan", "queued"): "building", ("build", "queued"): "building",
               ("reconcile", "queued"): "building", ("check", "queued"): "building",
               ("revert", "queued"): "building"}  # fmt: skip
NO_LEASE_STATES = {"needs_owner", "closed", "merged"}  # I6, and nothing to do


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def new_token() -> str:
    return secrets.token_hex(16)  # 128 bits


def _later(minutes: int) -> str:
    """now + minutes, in the clock's stamp format."""
    return clock.stamp_at(clock.now() + timedelta(minutes=minutes))


def expired(stamp: str, now: str | None = None) -> bool:
    return clock.parse(stamp) <= clock.parse(now or clock.stamp())


# --------------------------------------------------------------------------------------------------- unit leases
def active(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """unit key -> lease, for every unit holding one."""
    return {k: u["lease"] for k, u in state["units"].items() if u.get("lease")}


def can_grant(state: dict[str, Any], unit: str, kind: str) -> str | None:
    """None if a `kind` lease may be granted on `unit` now, else the reason it may not."""
    u = state["units"].get(unit)
    if u is None:
        return f"no unit {unit}"
    if u["lease"] is not None:
        return f"{unit} already holds a {u['lease']['kind']} lease"  # I1
    if u["state"] in NO_LEASE_STATES:
        return f"{unit} is {u['state']}"  # I6
    if u["state"] not in GRANTABLE[kind]:
        return f"a {kind} lease can't be granted on a unit in {u['state']}"
    if u["attempt"] >= MAX_ATTEMPTS:
        return f"{unit} has used {u['attempt']} attempts"
    return None


@dataclass(frozen=True)
class Grant:
    event: dict[str, Any]
    token: str  # goes into the brief, never into the store


def grant(state: dict[str, Any], unit: str, kind: str, *, epoch: int) -> Grant:
    """A `lease.granted` event for `unit` (raises ValueError if it can't be granted)."""
    reason = can_grant(state, unit, kind)
    if reason:
        raise ValueError(reason)
    attempt = state["units"][unit]["attempt"] + 1
    token = new_token()
    payload = {"kind": kind, "attempt": attempt, "expires_at": _later(EXPIRY_MIN[kind]), "holder_session": None,
               "lease_token_sha256": token_hash(token)}  # fmt: skip
    event = make_event("lease.granted", payload, writer="controller", unit=unit, epoch=epoch, attempt=attempt)
    return Grant(event, token)


def grant_in_scope(state: dict[str, Any], unit: str, kind: str, *, epoch: int, labels: list[str], paths: list[str],
                   cfg: Any) -> Grant:  # fmt: skip
    """`grant`, refused for a unit with an excluded label or only excluded paths (I9). The scheduler grants only
    through this, with the labels and paths from the tick's GitHub snapshot."""
    from loopctl.risk import excluded

    reason = excluded(labels, paths, cfg)
    if reason:
        raise ValueError(f"{unit} is out of scope: {reason}")
    return grant(state, unit, kind, epoch=epoch)


def renew(state: dict[str, Any], unit: str, *, writer: str, epoch: int | None = None,
          lease_token: str | None = None, claimant: str | None = None) -> dict[str, Any]:  # fmt: skip
    """A `lease.renewed` event: any call by the holder renews (§3.4); the controller may extend once."""
    lease = state["units"][unit]["lease"]
    if lease is None:
        raise ValueError(f"{unit} holds no lease")
    return make_event("lease.renewed", {"expires_at": _later(EXPIRY_MIN[lease["kind"]])}, writer=writer, unit=unit,
                      epoch=epoch, lease_token=lease_token, attempt=lease["attempt"], claimant=claimant)  # fmt: skip


def release(state: dict[str, Any], unit: str, reason: str, *, writer: str, epoch: int | None = None,
            lease_token: str | None = None) -> dict[str, Any]:  # fmt: skip
    lease = state["units"][unit]["lease"]
    if lease is None:
        raise ValueError(f"{unit} holds no lease")
    return make_event("lease.released", {"reason": reason}, writer=writer, unit=unit, epoch=epoch,
                      lease_token=lease_token, attempt=lease["attempt"])  # fmt: skip


@dataclass
class Expiry:
    events: list[dict[str, Any]] = field(default_factory=list)
    status_needed: list[str] = field(default_factory=list)  # sessions to ask the platform about (get_session)
    questions: list[str] = field(default_factory=list)  # units out of attempts: open a question (§3.4)


def expire(state: dict[str, Any], *, epoch: int, statuses: dict[str, str] | None = None,
           extended: set[str] | frozenset[str] = frozenset()) -> Expiry:  # fmt: skip
    """Leases past `expires_at`: ask the platform for the holder's status first; running -> extend once; otherwise
    release with `attempt + 1`, and after MAX_ATTEMPTS a question. `statuses` maps session -> platform status
    ("running", "idle", "archived", ...); `extended` lists units already extended once."""
    statuses = statuses or {}
    out = Expiry()
    now = clock.stamp()
    for key, lease in sorted(active(state).items()):
        if not expired(lease["expires_at"], now):
            continue
        holder = lease["holder_session"]
        status = statuses.get(holder) if holder else "unknown"
        if holder and status is None:
            out.status_needed.append(holder)
            continue
        if status == "running" and key not in extended:
            out.events.append(renew(state, key, writer="controller", epoch=epoch))
            continue
        out.events.append(make_event("lease.expired", {"attempt": lease["attempt"]}, writer="controller", unit=key,
                                     epoch=epoch, attempt=lease["attempt"]))  # fmt: skip
        if state["units"][key]["attempt"] + 1 >= MAX_ATTEMPTS:
            out.questions.append(key)
    return out


# ---------------------------------------------------------------------------------------------- controller lease
@dataclass(frozen=True)
class ControllerDecision:
    """What a tick in session `session` may do about the controller lease (§3.5).

    action: "take" (first start, lapsed or holder gone), "renew", "ask_holder" (print get_session on the holder and
    tick again with its status), "stand_down" (another live controller holds it), "refuse" (self-check failed).
    """

    action: str
    event: dict[str, Any] | None = None
    delete_tasks: tuple[str, ...] = ()  # tick tasks to delete (the previous controller's, or our own old one)
    create_task: bool = False  # print create_trigger for our tick task
    needs_you: str | None = None
    holder: str | None = None

    @property
    def holds(self) -> bool:
        return self.action in ("take", "renew")


def decide_controller(state: dict[str, Any], *, session: str, environment: str, self_check_ok: bool = True,
                      self_check_problem: str = "", holder_status: str | None = None,
                      tick_task: str | None = None) -> ControllerDecision:  # fmt: skip
    """The seven cases of §3.5 as one pure decision. `holder_status` is the platform's status of another holder
    (from `get_session`, through `loopctl ack`); None means not asked yet. `tick_task` is our task id once known."""
    lease = state.get("controller")
    previous = state.get("previous_controller")
    stale_own_task: tuple[str, ...] = ()
    if previous and previous["session"] == session and previous.get("tick_task"):
        stale_own_task = (previous["tick_task"],)  # we were taken over: delete our own old task
    if not self_check_ok:
        return ControllerDecision("refuse", needs_you=f"controller self-check failed: {self_check_problem}; "
                                  "see docs/v2/controller-runbook.md", delete_tasks=stale_own_task)  # fmt: skip
    now = clock.stamp()

    def take(reason: str) -> ControllerDecision:
        epoch = (lease["epoch"] if lease else 0) + 1
        payload = {"session": session, "environment": environment, "epoch": epoch, "granted_at": now,
                   "expires_at": _later(CONTROLLER_LEASE_MIN), "tick_task": None, "reason": reason}  # fmt: skip
        old = (lease["tick_task"],) if lease and lease.get("tick_task") and lease["session"] != session else ()
        event = make_event("controller.lease", payload, writer="controller", epoch=epoch)
        return ControllerDecision("take", event, delete_tasks=old, create_task=True)

    if lease is None:
        return take("first")
    if lease["session"] == session:
        if lease["environment"] != environment:
            return ControllerDecision("refuse", needs_you=f"controller environment changed from "
                                      f"{lease['environment']} to {environment}")  # fmt: skip
        payload = dict(lease, expires_at=_later(CONTROLLER_LEASE_MIN), reason="renew")
        if tick_task and not lease.get("tick_task"):
            payload["tick_task"] = tick_task
        event = make_event("controller.lease", payload, writer="controller", epoch=lease["epoch"])
        return ControllerDecision("renew", event, create_task=not (lease.get("tick_task") or tick_task))
    if expired(lease["expires_at"], now):
        return take("expired")
    if holder_status is None:
        return ControllerDecision("ask_holder", holder=lease["session"], delete_tasks=stale_own_task)
    if holder_status in ("archived", "gone", "not_found", "deleted"):
        return take("holder_gone")
    return ControllerDecision("stand_down", delete_tasks=stale_own_task, holder=lease["session"],
                              needs_you=f"two controllers: {lease['session']} holds the lease and is "
                              f"{holder_status}; archive the old one")  # fmt: skip
