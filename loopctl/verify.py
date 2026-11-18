"""`loopctl verify`: the decisions of the verify workflow, `agent-loop.yml` (phase2-design §10.2–10.4, §4.5, §12.3;
decision records 0022, 0024, 0026; proof tests T-VERIFY, T-INV-11 c).

The workflow (template: `loopctl/templates/workflow/agent-loop.yml`) runs in the target repository, always from
`main`, with the loop App's installation token. It never checks out or runs PR code. Its shell steps only gather
facts with GitHub reads into a directory and carry out the plan this module prints; every decision is made here, by
pure functions over those facts, and this module itself opens no connection and runs no process (T-INV-11 a):

- **Merge requests** (`workflow_dispatch` with input `prs`, or the `merge-request` label): `merge_plan`. `STOP` is
  read first: while a line matches the gate kind, the unit's kind or the unit, a loop merge request is refused with
  "loop paused" (§4.5). While `state.main.green` is false (main is known red, §10.6) loop merges are refused except
  the exact revert PR (its parent is the red sha and its tree the last green tree). An owner's own merge request (the
  label added by the owner from GitHub itself, no app, §10.4) still merges through both: the owner acted in person;
  the preconditions still apply. For each PR: the store's current state (`state.json` plus the event files not yet
  folded in, as the tick folds them), the gate result file `gate/<H>.json` (it must match the unit's), main's gate
  command hash recomputed from main's `.agent-loop.toml` and main's gate script exactly as `loopctl gate` computes
  it (`gate.command_hash`, 0024), and `verdict.check_merge` preconditions 1, 2, 4 and 6 on the PR's head H now. A
  PR that passes gets `agent-loop/verify` = success on H and is merged by squash with `sha: H` (GitHub refuses if the
  head moved). Precondition 4 needs the gate's base to be main now, so after the first merge of a run main has moved
  and the rest of the batch is refused with "gate again on the new main".
- **Control issue comments** (`issue_comment`): `control_plan`. `/loop pause [kinds]` and `/loop stop <unit>` from
  the owner (no app) add their `STOP` line (an unparseable one adds `pause`: fail closed). `/loop confirm <id>
  <nonce>` (or `/loop refuse …`) from the owner, not through an app, for a command waiting in `state.json`
  `commands` with that nonce writes `confirmations/<id>.json` = `{command_id, nonce, decision, comment_url}` (the
  M0.7 format: the controller trusts it only from verified commits by `install.json` `app_login`); a confirmed
  `resume` also removes the `STOP` lines it covers (`commands.resume_covers`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loopctl import clock, commands, gate, stop
from loopctl.config import ConfigError, resolve
from loopctl.reducer import Policy, reduce
from loopctl.risk import _get
from loopctl.store import STATE_FILE, empty_state, read_event_files
from loopctl.verdict import VERIFY_PRECONDITIONS, check_merge

CONTEXT = "agent-loop/verify"
MERGE_LABEL = "merge-request"
APPROVAL_LABELS = ("merge-request", "owner-approved")
DESCRIPTION_MAX = 140  # GitHub's limit for a commit status description
TEMPLATE = Path(__file__).resolve().parent / "templates" / "workflow" / "agent-loop.yml"
CONTROL = re.compile(r"^/loop[ \t]+(\S+)(.*)$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
NONCE = re.compile(r"^\S{8,128}$")
PR_UNIT_STATES = ("in_review", "changes", "approved", "gating", "needs_owner")


class VerifyError(ValueError):
    """The facts are incomplete or malformed: nothing is merged or written."""


# ------------------------------------------------------------------------------------------------------- the store
def current_state(state: Mapping[str, Any], events: Sequence[Mapping[str, Any]], cfg: Any = None) -> dict[str, Any]:
    """`state.json` with the event files not yet folded in (workers append events; the controller writes state.json
    at its next tick). The reducer skips applied ids (I4), so this is what the next tick will see."""
    return reduce(dict(state), events, Policy(cfg=cfg)).state


def unit_for_pr(state: Mapping[str, Any], pr: int) -> dict[str, Any] | None:
    """The open unit that owns PR `pr` (a merged or closed unit no longer does)."""
    open_units = [u for _, u in sorted(state["units"].items()) if u.get("pr") == pr and u["state"] in PR_UNIT_STATES]
    return open_units[0] if open_units else None


# ------------------------------------------------------------------------------------------------- merge requests
@dataclass
class Facts:
    """What the workflow's verify step gathered with GitHub reads (`load_facts` reads the directory)."""

    main: str  # the target's main branch name
    main_sha: str
    project_text: str | None  # main's .agent-loop.toml
    main_files: Mapping[str, bytes]  # main's gate scripts, path -> bytes (a script main doesn't have is absent)
    state: Mapping[str, Any]  # state.json, events folded in
    gate_files: Mapping[str, Mapping[str, Any]]  # head -> gate/<head>.json
    stop_text: str | None
    prs: Mapping[int, Mapping[str, Any]]  # number -> GET /pulls/<n>
    commits: Mapping[str, Mapping[str, Any]]  # sha -> GET /commits/<sha>
    issue_events: Mapping[int, Sequence[Mapping[str, Any]]]  # number -> GET /issues/<n>/events
    event_name: str  # workflow_dispatch | pull_request_target
    requested: list[int]  # the PR numbers asked for
    owner: str  # the owner's GitHub login


@dataclass
class Plan:
    statuses: list[dict[str, str]] = field(default_factory=list)  # [{sha, state, description}] posted first
    merges: list[dict[str, Any]] = field(default_factory=list)  # [{pr, sha, main, description}]
    report: list[str] = field(default_factory=list)

    def doc(self) -> dict[str, Any]:
        return {"context": CONTEXT, "statuses": self.statuses, "merges": self.merges, "report": self.report}


def parse_prs(text: str, limit: int) -> list[int]:
    """The `prs` input: PR numbers separated by commas or spaces, deduplicated in order, at most `limit`."""
    out: list[int] = []
    for word in re.split(r"[,\s]+", text.strip()):
        if not word:
            continue
        if not re.fullmatch(r"[1-9][0-9]{0,8}", word):
            raise VerifyError(f"{word!r} is not a PR number")
        if int(word) not in out:
            out.append(int(word))
    if not out:
        raise VerifyError("no PR numbers given")
    if len(out) > limit:
        raise VerifyError(f"{len(out)} PRs given; merge.batch_max is {limit}")
    return out


def script_paths(project_text: str | None) -> list[str]:
    """main's gate scripts the verify step must fetch: those of `project.gate` and `project.gate_docs`."""
    out = []
    for tier in (None, "docs"):
        try:
            script = gate.script_of(gate.gate_command(project_text, tier))
        except gate.GateProblem:
            continue
        if script and script not in out:
            out.append(script)
    return out


def main_command_hash(project_text: str | None, tier: str | None, main_files: Mapping[str, bytes]) -> str:
    """main's gate command hash, computed as `loopctl gate` computes it (gate.command_hash over main's command and
    main's copy of its script). Raises gate.GateProblem when main configures no gate."""
    command = gate.gate_command(project_text, tier)
    script = gate.script_of(command)
    return gate.command_hash(command, main_files.get(script) if script else None)


def owner_label(events: Sequence[Mapping[str, Any]], owner: str, labels: Sequence[str], after: str | None) -> bool:
    """Whether the owner added one of `labels` from GitHub itself (no app, §10.4) after `after` (H's commit time)."""
    for e in events:
        if e.get("event") != "labeled" or (e.get("label") or {}).get("name") not in labels:
            continue
        if (e.get("actor") or {}).get("login") != owner or e.get("performed_via_github_app"):
            continue
        if after is None or not e.get("created_at") or clock.parse(e["created_at"]) <= clock.parse(after):
            continue
        return True
    return False


def latest_label(events: Sequence[Mapping[str, Any]], label: str) -> Mapping[str, Any] | None:
    found = [e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    return found[-1] if found else None


def _commit_time(commit: Mapping[str, Any] | None) -> str | None:
    c = (commit or {}).get("commit") or {}
    return (c.get("committer") or {}).get("date") or (c.get("author") or {}).get("date")


def exact_revert(unit: Mapping[str, Any], head: str, commits: Mapping[str, Mapping[str, Any]]) -> str | None:
    """None when H is the exact revert of the unit (parent = the red sha, tree = the last green sha's tree), else
    why not (§10.6)."""
    rv = unit.get("revert") or {}
    if unit.get("kind") != "revert" or not rv.get("red") or not rv.get("good"):
        return "it is not the revert unit's PR"
    h, good = commits.get(head), commits.get(rv["good"])
    if not h or not good:
        return "its commits couldn't be read"
    parents = [p.get("sha") for p in h.get("parents") or []]
    if parents != [rv["red"]]:
        return f"its parent is not the red sha {rv['red'][:12]}"
    tree = lambda c: ((c.get("commit") or {}).get("tree") or {}).get("sha")  # noqa: E731
    if not tree(h) or tree(h) != tree(good):
        return f"its tree is not the tree of the last green sha {rv['good'][:12]}"
    return None


def _status(plan: Plan, sha: str, ok: bool, text: str) -> None:
    plan.statuses.append({"sha": sha, "state": "success" if ok else "failure", "description": text[:DESCRIPTION_MAX]})


def merge_plan(f: Facts) -> Plan:
    """The statuses to post and the merges to make (pure)."""
    plan = Plan()
    try:
        cfg = resolve(f.project_text, project_file="main:.agent-loop.toml")
    except ConfigError as exc:
        plan.report.append(f"main's .agent-loop.toml is invalid, nothing merges: {exc}"[:500])
        cfg = None
    lines = stop.parse(f.stop_text)
    red = f.state["main"].get("green") is False
    merged: int | None = None
    for n in f.requested:
        pr = f.prs.get(n)
        if not pr:
            plan.report.append(f"#{n}: GitHub doesn't list it")
            continue
        head = pr["head"]["sha"]
        if pr.get("state") != "open" or pr.get("merged"):
            plan.report.append(f"#{n}: not open")
            continue

        def refuse(why: str, n: int = n, head: str = head) -> None:
            _status(plan, head, False, why)
            plan.report.append(f"#{n} ({head[:12]}): refused: {why}")

        if cfg is None:
            refuse("main's .agent-loop.toml is invalid")
            continue
        if (pr.get("base") or {}).get("ref") != f.main:
            refuse(f"the PR is not into {f.main}")
            continue
        unit = unit_for_pr(f.state, n)
        if unit is None:
            refuse("no open loop unit owns this PR")
            continue
        events = f.issue_events.get(n, ())
        commit_time = _commit_time(f.commits.get(head))
        by_owner = False
        if f.event_name == "pull_request_target":
            last = latest_label(events, MERGE_LABEL)
            by_owner = bool(last) and owner_label([last], f.owner, (MERGE_LABEL,), commit_time)
        if not by_owner:  # a loop merge request: STOP and a red main stop it
            line = stop.matching(lines, "gate", unit["key"]) or stop.matching(lines, unit["kind"], unit["key"])
            if line is not None:
                refuse(f"loop paused: STOP has {line!r}")
                continue
            if red:
                why = exact_revert(unit, head, f.commits)
                if why is not None:
                    refuse(f"main is red: only the exact revert PR merges ({why})")
                    continue
        try:
            chash = main_command_hash(f.project_text, unit.get("tier"), f.main_files)
        except gate.GateProblem as exc:
            refuse(str(exc))
            continue
        approvals = list(unit.get("approvals") or [])
        if owner_label(events, f.owner, APPROVAL_LABELS, commit_time):
            approvals.append(head)
        check = check_merge(unit, pr_head=head, cfg=cfg, main_sha=f.main_sha, gate_command_sha256=chash,
                            approvals=approvals, checks=VERIFY_PRECONDITIONS)  # fmt: skip
        if not check.ok:
            refuse(check.reason())
            continue
        gate_doc, mine = f.gate_files.get(head), unit.get("gate") or {}
        if gate_doc is None:
            refuse(f"no gate result file gate/{head[:12]}… in the store")
            continue
        if any(gate_doc.get(k) != mine.get(k) for k in ("head", "tested", "base", "result", "gate_command_sha256")):
            refuse("the gate result file doesn't match the unit's gate result")
            continue
        if merged is not None:
            refuse(f"main moves with the merge of #{merged} in this run: gate again on the new main")
            continue
        merged = n
        text = "merge preconditions 1, 2, 4, 6 hold" + (" (owner's merge request)" if by_owner else "")
        plan.merges.append({"pr": n, "sha": head, "main": f.main_sha, "description": text})
        plan.report.append(f"#{n} ({head[:12]}): {text}: squash-merging with sha {head[:12]}")
    return plan


def load_facts(root: Path, *, event_name: str, prs_text: str, owner: str) -> Facts:
    """Read the directory the verify step filled (layout in the workflow template)."""
    root = Path(root)

    def text(rel: str) -> str | None:
        p = root / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def jsonl(p: Path) -> list[Any]:  # a stream of JSON values (gh prints one per `--jq '.[]'` result)
        raw, out, i, dec = p.read_text(encoding="utf-8"), [], 0, json.JSONDecoder()
        while True:
            while i < len(raw) and raw[i].isspace():
                i += 1
            if i >= len(raw):
                return out
            value, i = dec.raw_decode(raw, i)
            out.append(value)

    state_dir = root / "state"
    raw_state = text(f"state/{STATE_FILE}")
    project_text = text("main/.agent-loop.toml")
    try:
        project_cfg = resolve(project_text, project_file="main:.agent-loop.toml")
    except ConfigError:
        project_cfg = None
    base_state = json.loads(raw_state) if raw_state is not None else empty_state()
    try:
        cfg = commands.effective(project_text, base_state, "main:.agent-loop.toml")
    except Exception:  # noqa: BLE001 - invalid overrides: fold with main's own config
        cfg = project_cfg
    state = current_state(base_state, _recent_events(state_dir), cfg)
    limit = int(_get(project_cfg, "merge.batch_max")) if project_cfg is not None else 1
    requested = parse_prs(prs_text, limit)
    main_files = {}
    for path in script_paths(project_text):
        p = root / "main" / path
        if p.is_file():
            main_files[path] = p.read_bytes()
    gate_files = {}
    for p in sorted((state_dir / "gate").glob("*.json")) if (state_dir / "gate").is_dir() else []:
        try:
            gate_files[p.stem] = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
    prs = {}
    for p in sorted((root / "prs").glob("*.json")):
        prs[int(p.stem)] = json.loads(p.read_text(encoding="utf-8"))
    events = {int(p.stem): jsonl(p) for p in sorted((root / "events").glob("*.jsonl"))}
    commits = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted((root / "commits").glob("*.json"))}
    main_sha = (text("main_sha") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", main_sha):
        raise VerifyError("main's sha wasn't read")
    return Facts(main=_get(project_cfg, "project.main") if project_cfg is not None else "main", main_sha=main_sha,
                 project_text=project_text, main_files=main_files, state=state, gate_files=gate_files,
                 stop_text=text(f"state/{stop.FILE}"), prs=prs, commits=commits, issue_events=events,
                 event_name=event_name, requested=requested, owner=owner)  # fmt: skip


def _recent_events(state_dir: Path) -> list[dict[str, Any]]:
    from loopctl.store import recent_event_files

    return recent_event_files(state_dir) if (state_dir / "events").is_dir() else read_event_files(state_dir, [])


def extra_commits(state: Mapping[str, Any], requested: Sequence[int]) -> list[str]:
    """Commits the verify step must also read: the last green sha of a requested revert unit (its tree)."""
    out = []
    for n in requested:
        unit = unit_for_pr(state, n)
        good = ((unit or {}).get("revert") or {}).get("good")
        if good and good not in out:
            out.append(good)
    return out


# ---------------------------------------------------------------------------------------------- the control issue
def control_plan(event: Mapping[str, Any], state: Mapping[str, Any], *, owner: str, control_issue: int,
                 app_slug: str | None, install: Mapping[str, Any] | None,
                 existing: Sequence[str] = ()) -> dict[str, Any]:  # fmt: skip
    """What one comment on the control issue does (pure): `{action, line?, confirmation?, resume?, report}`.
    `existing`: confirmation files already in the state repo (`confirmations/<id>.json` names)."""
    comment, issue = event.get("comment") or {}, event.get("issue") or {}
    out: dict[str, Any] = {"action": "none", "report": []}

    def no(why: str) -> dict[str, Any]:
        out["report"].append(why)
        return out

    if issue.get("number") != control_issue or issue.get("pull_request"):
        return no("not the control issue")
    first = (comment.get("body") or "").strip().splitlines()[0:1]
    m = CONTROL.match(first[0].strip()) if first else None
    if not m:
        return no("not a /loop command")
    verb, rest = m.group(1), m.group(2).split()
    author = (comment.get("user") or {}).get("login")
    if author != owner:
        return no(f"/loop {verb} from {author or 'nobody'}: only the owner ({owner}) is obeyed")
    if comment.get("performed_via_github_app"):
        app = (comment["performed_via_github_app"] or {}).get("slug") or "an app"
        return no(f"/loop {verb} was posted through {app}: only the owner acting in GitHub itself is obeyed (§10.4)")
    url = comment.get("html_url") if isinstance(comment.get("html_url"), str) else None
    if verb in ("pause", "stop"):
        line = stop.parse_line(" ".join([verb, *rest]))
        text = "pause"
        if line is not None and line.valid:  # written in the canonical form (kinds in STOP_KINDS order)
            text = f"stop {line.unit}" if line.unit else " ".join(["pause", *line.kinds]) if line.kinds else line.line
        if text == "pause" and (line is None or not line.valid):
            out["report"].append(f"'/loop {verb} {' '.join(rest)}' doesn't parse: adding 'pause' (fail closed)")
        out.update(action="stop", line=text, comment_url=url)
        out["report"].append(f"adding STOP line {text!r}")
        return out
    if verb in ("confirm", "refuse"):
        if len(rest) != 2 or not TOKEN.match(rest[0]) or not NONCE.match(rest[1]):
            return no(f"usage: /loop {verb} <command id> <nonce>")
        cid, nonce = rest
        rec = (state.get("commands") or {}).get(cid)
        if not rec:
            return no(f"no command {cid} in the store")
        if rec.get("status") != commands.AWAITING:
            return no(f"command {cid} is {rec.get('status')!r}, not waiting for a confirmation")
        if rec.get("nonce") != nonce:
            return no(f"that nonce isn't command {cid}'s")
        if f"{cid}.json" in existing:
            return no(f"command {cid} already has a confirmation file")
        app_login = (install or {}).get("app_login")
        if not app_slug or app_login != f"{app_slug}[bot]":
            return no(f"install.json app_login is {app_login!r}, but this App commits as {app_slug}[bot]: the "
                      "controller wouldn't trust the confirmation, so none is written")  # fmt: skip
        decision = "confirm" if verb == "confirm" else "refuse"
        out.update(action=decision, confirmation={
            "path": f"{commands.CONFIRM_DIR}/{cid}.json",
            "doc": {"command_id": cid, "nonce": nonce, "decision": decision, "comment_url": url}})  # fmt: skip
        if decision == "confirm" and rec.get("type") == "resume":
            out["resume"] = {"type": "resume", "args": rec.get("args") or {}}
            out["report"].append(f"confirmed resume {cid}: removing the STOP lines it covers")
        out["report"].append(f"writing {commands.CONFIRM_DIR}/{cid}.json ({decision})")
        return out
    return no(f"/loop {verb}: not a command this workflow handles (pause, stop, confirm, refuse)")


def stop_edit(control: Mapping[str, Any], stop_text: str | None) -> str:
    """The new STOP text for a control plan: its line added (once), or a confirmed resume's covered lines removed.
    Other lines are kept (blank lines, which say nothing, are dropped); a resume with kinds never removes an
    unparseable line."""
    lines = (stop_text or "").splitlines()
    if control.get("action") == "stop" and control.get("line"):
        if control["line"] in [x.strip() for x in lines]:
            return stop_text or ""
        kept = [x for x in lines if x.strip()]
        return "\n".join([*kept, control["line"]]) + "\n"
    if control.get("resume"):
        kept = [x for x in lines if x.strip() and not commands.resume_covers(control["resume"], x)]
        return "\n".join(kept) + "\n" if kept else ""
    return stop_text or ""


# ------------------------------------------------------------------------------------------------- the template
PLACEHOLDERS = ("__STATE_REPO__", "__STATE_NAME__", "__ENGINE_REPO__", "__ENGINE_NAME__", "__ENGINE_SHA__",
                "__OWNER_LOGIN__", "__CONTROL_ISSUE__", "__MAIN__", "__APP_REPOS__", "__APP_REPOS_CONTROL__")  # fmt: skip
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")


def render(*, target: str, state_repo: str, engine_repo: str, engine_sha: str, owner_login: str, control_issue: int,
           main: str = "main", template: str | None = None) -> str:  # fmt: skip
    """The workflow file for one installation (M0.13's setup writes it to the target's `.github/workflows/`). The
    App token is minted for the repositories named here only; they must share one owner (one installation)."""
    for name, repo in (("target", target), ("state repo", state_repo), ("engine", engine_repo)):
        if not REPO.match(repo):
            raise ValueError(f"the {name} {repo!r} is not owner/name")
    owners = {r.split("/")[0].lower() for r in (target, state_repo, engine_repo)}
    if len(owners) != 1:
        raise ValueError("the target, state and engine repositories must have one owner (one App installation)")
    if not re.fullmatch(r"[0-9a-f]{40}", engine_sha):
        raise ValueError("the engine is pinned by a full commit sha")
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner_login):
        raise ValueError(f"{owner_login!r} is not a GitHub login")
    if not isinstance(control_issue, int) or control_issue < 1:
        raise ValueError("the control issue is a positive issue number")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", main):
        raise ValueError(f"{main!r} is not a branch name")
    names = list(dict.fromkeys(r.split("/")[1] for r in (target, state_repo, engine_repo)))
    control_names = list(dict.fromkeys(r.split("/")[1] for r in (state_repo, engine_repo)))
    values = {"__STATE_REPO__": state_repo, "__STATE_NAME__": state_repo.split("/")[1], "__ENGINE_REPO__": engine_repo,
              "__ENGINE_NAME__": engine_repo.split("/")[1], "__ENGINE_SHA__": engine_sha, "__OWNER_LOGIN__": owner_login,
              "__CONTROL_ISSUE__": str(control_issue), "__MAIN__": main, "__APP_REPOS__": ",".join(names),
              "__APP_REPOS_CONTROL__": ",".join(control_names)}  # fmt: skip
    text = TEMPLATE.read_text(encoding="utf-8") if template is None else template
    for k, v in values.items():
        text = text.replace(k, v)
    return text


# ------------------------------------------------------------------------------------------------------- the CLI
def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="verify_command", metavar="<step>", required=True)
    s = sub.add_parser("scripts", help="main's gate scripts to fetch, one path per line")
    s.add_argument("--project", required=True, help="main's .agent-loop.toml (may not exist)")
    s = sub.add_parser("prs", help="the requested PR numbers, one per line (checked against merge.batch_max)")
    s.add_argument("--project", required=True)
    s.add_argument("--prs", required=True, help="the dispatch input `prs` or the labeled PR's number")
    s = sub.add_parser("commits", help="other commits to read (a revert unit's last green sha)")
    s.add_argument("--facts", required=True)
    s.add_argument("--prs", required=True)
    s = sub.add_parser("merge", help="the merge plan as JSON: statuses to post, merges to make")
    s.add_argument("--facts", required=True)
    s.add_argument("--prs", required=True)
    s.add_argument("--event", required=True, choices=["workflow_dispatch", "pull_request_target"])
    s.add_argument("--owner", required=True)
    s = sub.add_parser("control", help="what a control issue comment does, as JSON")
    s.add_argument("--event-file", required=True, help="$GITHUB_EVENT_PATH")
    s.add_argument("--state-dir", required=True, help="the state repo's clone")
    s.add_argument("--owner", required=True)
    s.add_argument("--control-issue", type=int, required=True)
    s.add_argument("--app-slug", default="")
    s = sub.add_parser("stop-edit", help="print the new STOP text for a control plan")
    s.add_argument("--control", required=True, help="the control plan JSON")
    s.add_argument("--stop", required=True, help="the current STOP text (an empty file when there is none)")


def _read(path: str) -> str | None:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.is_file() else None


def run(args: argparse.Namespace) -> int:
    try:
        return _run(args)
    except (VerifyError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f"loopctl verify {args.verify_command}: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    cmd = args.verify_command
    if cmd == "scripts":
        for path in script_paths(_read(args.project)):
            print(path)
        return 0
    if cmd == "prs":
        try:
            limit = int(_get(resolve(_read(args.project), project_file="main:.agent-loop.toml"), "merge.batch_max"))
        except ConfigError:
            limit = 1
        for n in parse_prs(args.prs, limit):
            print(n)
        return 0
    if cmd == "commits":
        root = Path(args.facts)
        raw = _read(str(root / "state" / STATE_FILE))
        state = current_state(json.loads(raw) if raw else empty_state(), _recent_events(root / "state"))
        for sha in extra_commits(state, parse_prs(args.prs, 20)):
            print(sha)
        return 0
    if cmd == "merge":
        facts = load_facts(Path(args.facts), event_name=args.event, prs_text=args.prs, owner=args.owner)
        print(json.dumps(merge_plan(facts).doc(), indent=1, ensure_ascii=False))
        return 0
    if cmd == "control":
        event = json.loads(Path(args.event_file).read_text(encoding="utf-8"))
        state_dir = Path(args.state_dir)
        raw = _read(str(state_dir / STATE_FILE))
        install_raw = _read(str(state_dir / "install.json"))
        conf = state_dir / commands.CONFIRM_DIR
        existing = sorted(p.name for p in conf.glob("*.json")) if conf.is_dir() else []
        plan = control_plan(event, json.loads(raw) if raw else empty_state(), owner=args.owner,
                            control_issue=args.control_issue, app_slug=args.app_slug or None,
                            install=json.loads(install_raw) if install_raw else None, existing=existing)  # fmt: skip
        print(json.dumps(plan, indent=1, ensure_ascii=False))
        return 0
    if cmd == "stop-edit":
        control = json.loads(Path(args.control).read_text(encoding="utf-8"))
        sys.stdout.write(stop_edit(control, _read(args.stop)))
        return 0
    raise VerifyError(f"unknown step {cmd}")
