"""The worker commands (phase2-design §3.2, §9.2, §9.4; decision record 0029; proof tests T-INV-8, T-GATE), in exactly
the forms the briefs give (`brief.commands()`):

    loopctl guard identify <kind> <unit> --token T      (the claim only: `claim_main`)
    loopctl event <unit> --token T fix-pushed --pr <n> --head <sha>
    loopctl event <unit> --token T finding-fixed --finding <f-id> --commit <sha> --test '<test>'
    loopctl event <unit> --token T triage-done --verdict <v> [--priority <P0..P3>] [--duplicate-of <n>] [--frozen]
    loopctl event <unit> --token T reconcile-report --file <report.json>   (`loopctl reconcile --out`'s file)
    loopctl event <unit> --token T revert-pushed --pr <n> --head <sha>
    loopctl review submit <unit> --token T --head <sha> --file <findings.json>
    loopctl gate <unit> --token T [--target-dir <clone>]
    loopctl finish <unit> --token T --outcome <done|blocked|no-progress> --note '<one line>'
    loopctl finish <unit> --token T --stopped

Every command presents the unit's raw lease token. It is checked twice: here, against the lease in the freshly synced
store (sha256 of the token = the lease's `lease_token_sha256`, and the writer's kind = the lease's kind), before
anything is written and again before each push attempt; and by the reducer when the controller applies the event
(I8).

The first unit-scoped call of a session also **claims** the lease (`loopctl/claim.py`, decision record 0029): it
writes `claims/<lease_token_sha256>.json` with the calling session's identity in the same compare-and-swap commit as
its events (or alone, from `loopctl guard identify <kind> <unit> --token T`, the first command of every brief). A call
with the same token from any other session (a duplicate start of the unit) is refused before anything is written,
and the guard then holds that session stopped. Every event carries the claimant's sha256, and the reducer refuses a
worker event whose claimant differs from the one it recorded.

The events are also reduced here first, so a refusal (wrong state, wrong head, over the round cap) is reported to the
worker instead of being written. A worker writes event files (and `gate/<H>.json`, `claims/`) only, never
`state.json`.
No command takes a time: code stamps every time (§11).

Exit codes: 0 recorded, 2 usage, 3 refused (nothing written), 1 the store or the gate couldn't be used.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from loopctl import claim, clock, leases
from loopctl.store import StoreError, dumps, make_event

REFUSED = 3
EVENT_TYPES = {"fix-pushed": "fix.pushed", "finding-fixed": "finding.fixed", "triage-done": "triage.done",
               "reconcile-report": "reconcile.reported", "revert-pushed": "revert.pushed"}  # fmt: skip
OUTCOMES = ("done", "blocked", "no-progress")


class Refused(Exception):
    """The command can't be accepted; nothing was written."""


class ClaimTaken(Refused):
    """Another session claimed the lease first (a duplicate start): this session must stop."""


# ------------------------------------------------------------------------------------------------------- arguments
def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("unit", help="the unit, as in your brief (issue-12, pr-40, …)")
    p.add_argument("--token", required=True, help="the lease token from your brief")
    p.add_argument("--state", default=None,
                   help="the state repo's clone (default: $LOOPCTL_STATE_DIR, else the attached checkout, else .)")
    p.add_argument("--project", default=None,
                   help="the target's .agent-loop.toml (default: $LOOPCTL_PROJECT, else the attached target's)")


def add_arguments(sub: Any) -> None:
    event = sub.add_parser("event", help="record a worker event on your unit (in the form your brief gives)")
    _common(event)
    event.add_argument("type", choices=sorted(EVENT_TYPES), help="what happened")
    event.add_argument("--pr", type=int)
    event.add_argument("--head")
    event.add_argument("--finding")
    event.add_argument("--commit")
    event.add_argument("--test")
    event.add_argument("--verdict", choices=["accepted", "rejected", "deferred", "duplicate"])
    event.add_argument("--priority", choices=["P0", "P1", "P2", "P3"])
    event.add_argument("--duplicate-of", type=int, dest="duplicate_of")
    event.add_argument("--frozen", action="store_true")
    event.add_argument("--file")
    finish = sub.add_parser("finish", help="end your unit's lease with an outcome and a one-line note")
    _common(finish)
    how = finish.add_mutually_exclusive_group(required=True)
    how.add_argument("--outcome", choices=OUTCOMES)
    how.add_argument("--stopped", action="store_true", help="the loop is paused: record the handoff and end")
    finish.add_argument("--note", default="", help="one line, at most 300 characters")
    review = sub.add_parser("review", help="submit a review's findings")
    rsub = review.add_subparsers(dest="review_command", metavar="submit", required=True)
    submit = rsub.add_parser("submit", help="submit every finding of this round in one file")
    _common(submit)
    submit.add_argument("--head", required=True, help="the PR head you reviewed")
    submit.add_argument("--file", required=True, help="a JSON list of findings (no ids: loopctl assigns them)")
    gate = sub.add_parser("gate", help="run the project's gate on your unit and record the result")
    _common(gate)
    gate.add_argument("--target-dir", default=None,
                      help="the target repository's clone (default: $LOOPCTL_TARGET_DIR, else the attached checkout, "
                           "else .)")  # fmt: skip


# ------------------------------------------------------------------------------------------------------------ core
def check_token(state: dict[str, Any], unit: str, token: str) -> dict[str, Any]:
    """The unit's lease, if `token` is its token (0029); else Refused."""
    u = state["units"].get(unit)
    if u is None:
        raise Refused(f"there is no unit {unit}")
    lease = u.get("lease")
    if lease is None:
        raise Refused(f"{unit} holds no lease (it expired, was finished or was taken over): stop working on it")
    if not isinstance(token, str) or leases.token_hash(token) != lease["lease_token_sha256"]:
        raise Refused(f"the token doesn't match {unit}'s current lease: this session doesn't hold it")
    return lease


def check_claim(text: str | None, unit: str, who: claim.Claimant) -> bool:
    """True when `who` already holds the lease's claim, False when nobody claimed it yet; ClaimTaken when another
    session did. `text` is the claim file as it is in the freshly synced store."""
    held = claim.holder(text)
    if held is None:
        return False
    if held != who.sha256:
        raise ClaimTaken(claim.taken_reason(unit))
    return True


class Session:
    """One worker command against the state repo: config, store, the lease check and the write."""

    def __init__(self, args: Any, store: Any = None, cfg_text: str | None = None) -> None:
        from loopctl.config import ConfigError, resolve

        self.args, self.unit, self.token = args, args.unit, args.token
        path = Path(args.project)
        self.project_text = cfg_text if cfg_text is not None else (
            path.read_text(encoding="utf-8") if path.exists() else None)  # fmt: skip
        try:
            self.base_cfg = resolve(self.project_text, project_file=str(path))
        except ConfigError:
            self.base_cfg = resolve()
        if store is None:
            from loopctl.controller_cli import _store

            store = _store(args.state, self.base_cfg)
        self.store = store
        try:
            self.who = claim.claimant()
        except claim.ClaimError as exc:
            raise StoreError(f"this session's identity for the lease claim couldn't be established: {exc}") from exc

    def fresh_check(self, state: dict[str, Any]) -> dict[str, str] | None:
        """On the freshly synced store, before every push attempt: the token (0029), then the claim. Returns the
        claim file to write with the events when nobody claimed the lease yet."""
        lease = check_token(state, self.unit, self.token)
        path = claim.path_for(lease)
        if check_claim(self.store.read_file(path), self.unit, self.who):
            return None
        return {path: claim.document(self.unit, lease, self.who)}

    def read(self) -> tuple[dict[str, Any], dict[str, Any], Any]:
        from loopctl import commands

        self.store.sync()
        state = self.store.read_state()
        lease = check_token(state, self.unit, self.token)
        check_claim(self.store.read_file(claim.path_for(lease)), self.unit, self.who)
        try:
            cfg = commands.effective(self.project_text, state, str(self.args.project))
        except Exception:  # noqa: BLE001 - an invalid override never blocks a worker's record
            cfg = self.base_cfg
        return state, lease, cfg

    def event(self, lease: dict[str, Any], type_: str, payload: dict[str, Any]) -> dict[str, Any]:
        return make_event(type_, payload, writer=f"worker:{lease['kind']}", unit=self.unit, lease_token=self.token,
                          attempt=lease["attempt"], claimant=self.who.sha256)  # fmt: skip

    def renew(self, state: dict[str, Any], lease: dict[str, Any]) -> dict[str, Any]:
        return leases.renew(state, self.unit, writer=f"worker:{lease['kind']}", lease_token=self.token,
                            claimant=self.who.sha256)  # fmt: skip

    def write(self, state: dict[str, Any], cfg: Any, events: list[dict[str, Any]], message: str,
              files: dict[str, str] | None = None) -> dict[str, Any]:  # fmt: skip
        """Reduce first (a refusal is reported, not written), then append with the token and the claim checked on
        fresh state (the claim file goes into the same commit on the first call)."""
        from loopctl.reducer import Policy, reduce

        result = reduce(state, events, Policy(cfg=cfg))
        if result.rejected:
            raise Refused(result.rejected[0][1])
        self.store.append_events(events, message, check=self.fresh_check, files=files)
        return result.state


def _print(doc: dict[str, Any]) -> None:
    print(json.dumps(doc, indent=1, ensure_ascii=False))


RECONCILE_MAX = 500  # mismatches kept in one reconcile.reported event (events.json maxItems)


def _read_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"{path} isn't a readable JSON file: {exc}") from exc


# -------------------------------------------------------------------------------------------------------- commands
def cmd_claim(sess: Session, kind: str) -> dict[str, Any]:
    """`loopctl guard identify <kind> <unit> --token T`: claim the lease for this session (nothing else is written).
    Claiming again from the same session changes nothing."""
    _state, lease, _cfg = sess.read()
    if lease["kind"] != kind:
        raise Refused(f"{sess.unit}'s lease is a {lease['kind']} lease, not a {kind} lease")
    sess.store.append_events([], f"loopctl claim {sess.unit}", check=sess.fresh_check)
    return {"claimed": sess.unit, "kind": kind, "attempt": lease["attempt"], "claimant": sess.who.source}


def cmd_event(sess: Session) -> dict[str, Any]:
    a = sess.args
    state, lease, cfg = sess.read()
    type_ = EVENT_TYPES[a.type]

    def need(*names: str) -> None:
        missing = [f"--{n.replace('_', '-')}" for n in names if getattr(a, n) in (None, "")]
        if missing:
            raise Refused(f"`event {a.type}` needs {', '.join(missing)}")

    if a.type in ("fix-pushed", "revert-pushed"):
        need("pr", "head")
        payload: dict[str, Any] = {"pr": a.pr, "head": a.head}
    elif a.type == "finding-fixed":
        need("finding", "commit", "test")
        payload = {"finding_id": a.finding, "commit": a.commit, "test": a.test}
    elif a.type == "triage-done":
        need("verdict")
        if a.verdict == "accepted" and not a.priority:
            raise Refused("an accepted issue needs --priority")
        if a.verdict == "duplicate" and not a.duplicate_of:
            raise Refused("a duplicate needs --duplicate-of")
        payload = {"verdict": a.verdict, "priority": a.priority, "duplicate_of": a.duplicate_of, "frozen": a.frozen}
    else:  # reconcile-report
        need("file")
        doc = _read_json(a.file)
        payload = {"mismatches": doc if isinstance(doc, list) else (doc or {}).get("mismatches")}
        if isinstance(doc, dict):  # `loopctl reconcile --out`'s report: also how much it compared and what waits
            if isinstance(doc.get("checked"), dict):
                payload["checked"] = sum(v for v in doc["checked"].values() if isinstance(v, int))
            if isinstance(doc.get("pending"), list):
                payload["pending"] = len(doc["pending"])
        if isinstance(payload["mismatches"], list) and len(payload["mismatches"]) > RECONCILE_MAX:
            payload["total"] = len(payload["mismatches"])  # the event keeps the first RECONCILE_MAX; the count stays
            payload["mismatches"] = payload["mismatches"][:RECONCILE_MAX]
    try:
        ev = sess.event(lease, type_, payload)
    except ValueError as exc:  # the payload fails the schema
        raise Refused(str(exc)) from exc
    events = [sess.renew(state, lease), ev] if type_ not in ("triage.done", "reconcile.reported") else [ev]
    after = sess.write(state, cfg, events, f"loopctl event {a.type} {sess.unit}")
    return {"recorded": type_, "unit": sess.unit, "state": after["units"][sess.unit]["state"]}


def cmd_finish(sess: Session) -> dict[str, Any]:
    a = sess.args
    state, lease, cfg = sess.read()
    outcome = "stopped" if a.stopped else a.outcome
    payload = {"reason": "stopped" if a.stopped else "finished", "outcome": outcome, "note": (a.note or "")[:300]}
    after = sess.write(state, cfg, [sess.event(lease, "lease.released", payload)], f"loopctl finish {sess.unit}")
    return {"recorded": "lease.released", "unit": sess.unit, "outcome": outcome,
            "state": after["units"][sess.unit]["state"], "next": "end this session"}  # fmt: skip


def cmd_review_submit(sess: Session) -> dict[str, Any]:
    from loopctl.findings import review_submit

    a = sess.args
    state, lease, cfg = sess.read()
    unit = state["units"][sess.unit]
    if a.head != unit["head"]:
        raise Refused(f"you reviewed {a.head[:12]}, the PR's head in the store is {str(unit['head'])[:12]}: the PR "
                      "moved; finish with outcome no-progress")  # fmt: skip
    doc = _read_json(a.file)
    found = doc if isinstance(doc, list) else (doc or {}).get("findings") if isinstance(doc, dict) else None
    if not isinstance(found, list):
        raise Refused("the findings file must be a JSON list of findings (or {\"findings\": [...]})")
    findings = []
    for f in found:
        if not isinstance(f, dict):
            raise Refused("every finding must be a JSON object")
        if "id" in f:
            raise Refused("findings are submitted without `id`: loopctl assigns it")
        findings.append({"id": "f-" + clock.ulid(), "line": None, **f})
    try:
        sub = review_submit({"pr": unit["pr"], "head": a.head, "round": unit["round"] + 1, "findings": findings},
                            unit, cfg)  # fmt: skip
    except ValueError as exc:
        raise Refused(str(exc)) from exc
    events = [sess.renew(state, lease), sess.event(lease, "review.submitted", sub.payload)]
    after = sess.write(state, cfg, events, f"loopctl review submit {sess.unit}")
    file_as_issues = [dict(f) for f in findings if sub.routes[f["id"]] == "issue"]
    return {"recorded": "review.submitted", "unit": sess.unit, "round": sub.payload["round"],
            "verdict": after["units"][sess.unit]["state"], "routes": sub.routes,
            "file_as_issues": file_as_issues}  # fmt: skip


def cmd_gate(sess: Session, io: Any = None, scratch: Path | None = None) -> dict[str, Any]:
    from loopctl import gate
    from loopctl.gate_io import GateIO
    from loopctl.reducer import Policy, reduce
    from loopctl.risk import _get
    from loopctl.verdict import check_merge

    io = io or GateIO()
    a = sess.args
    state, lease, cfg = sess.read()
    unit = state["units"][sess.unit]
    want = "check" if unit["kind"] == "check" else "gate"
    if lease["kind"] != want:
        raise Refused(f"{sess.unit}'s lease is a {lease['kind']} lease, not a {want} lease")
    if scratch is None:
        from loopctl.tick import scratch_dir

        scratch = scratch_dir()
    target = gate.Target.of(unit)
    main = _get(cfg, "project.main")
    repo = Path(a.target_dir)
    r = gate.run(io, repo, target, main=main, scratch=scratch, session=lease.get("holder_session"))
    res = r.result
    files = {gate.FILE.format(head=target.head): dumps(res)}
    if target.kind == "check":
        events = [sess.event(lease, "main.checked", {"result": res})]
    elif res["result"] == "pass" and not r.main_moved:
        events = [sess.renew(state, lease), sess.event(lease, "gate.passed", {"result": res})]
    else:
        events = [sess.renew(state, lease), sess.event(lease, "gate.failed", {"result": res,
                                                                             "attributed": r.attributed})]
    report: dict[str, Any] = {"unit": sess.unit, "result": res["result"], "head": res["head"], "tested": res["tested"],
                              "base": res["base"], "log": str(r.log_path), "log_sha256": res["log_sha256"],
                              "gate_command_sha256": res["gate_command_sha256"]}  # fmt: skip
    if r.note:
        report["note"] = r.note
    if events[-1]["type"] == "gate.passed":
        after = reduce(state, events, Policy(cfg=cfg)).state
        main_now, pr_head = gate.remote_heads(io, repo, main, target.pr)
        check = check_merge(after["units"][sess.unit], pr_head=pr_head or "unknown", cfg=cfg, main_sha=main_now,
                            tested=res["tested"], main_green=gate.main_green_for(state, unit),
                            gate_command_sha256=res["gate_command_sha256"])  # fmt: skip
        if not check.ok:
            report["merge_request"] = None
            report["why_no_merge_request"] = check.reason()
        elif not _get(cfg, "merge.auto"):
            report["merge_request"] = None
            report["why_no_merge_request"] = "merge.auto is off: the owner merges with a command"
        else:
            events.append(sess.event(lease, "merge.requested", {"prs": [target.pr]}))
            report["merge_request"] = gate.merge_request(_get(cfg, "project.repo"), main, [target.pr])
    elif target.kind == "check":
        report["main"] = "green" if res["result"] == "pass" else "red: merges stop and the controller opens a revert"
    else:
        report["merge_request"] = None
        report["why_no_merge_request"] = ("main moved while the gate ran: the controller gates it again"
                                          if r.main_moved else "the gate failed")  # fmt: skip
    sess.write(state, cfg, events, f"loopctl gate {sess.unit}", files=files)
    return report


COMMANDS = {"event": cmd_event, "finish": cmd_finish, "gate": cmd_gate}


def _refuse(name: str, exc: Refused) -> int:
    if isinstance(exc, ClaimTaken):
        claim.mark_refused(str(exc))  # the guard holds the session stopped from now on
    print(f"loopctl {name}: refused: {exc}", file=sys.stderr)
    return REFUSED


def main(args: Any) -> int:
    from loopctl.gate import GateProblem

    from loopctl.install import fill_defaults

    name = "review submit" if args.command == "review" else args.command
    fill_defaults(args)
    try:
        sess = Session(args)
        fn = cmd_review_submit if args.command == "review" else COMMANDS[args.command]
        _print(fn(sess))
    except Refused as exc:
        return _refuse(name, exc)
    except (StoreError, GateProblem, OSError) as exc:
        print(f"loopctl {name}: {exc}", file=sys.stderr)
        return 1
    return 0


def claim_main(kind: str, unit: str, token: str, state: str | None = None, project: str | None = None) -> int:
    """The claim behind `loopctl guard identify <kind> <unit> --token T` (exit codes as the worker commands)."""
    from loopctl.install import fill_defaults

    args = fill_defaults(argparse.Namespace(command="claim", unit=unit, token=token, state=state, project=project))
    try:
        _print(cmd_claim(Session(args), kind))
    except Refused as exc:
        return _refuse("guard identify", exc)
    except (StoreError, OSError) as exc:
        print(f"loopctl guard identify: {exc}", file=sys.stderr)
        return 1
    return 0
