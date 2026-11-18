"""`loopctl gate`: the gate run, its result and the merge request (phase2-design §9.4, §10.2, §10.6; decision records
0011, 0022, 0023, 0024; proof test T-GATE).

For a PR unit (kinds fix, review and revert, in `gating` under a gate lease) the gate:

1. fetches main (B) and the PR's head H into the target's clone and builds **T = H squashed onto B** in a scratch
   worktree (a three-way merge, as GitHub's squash makes it, so main's tree after the merge equals T's tree: I3);
   a revert unit's H must be exactly the last green tree on top of the red sha (§10.6), or the gate fails;
2. takes the gate command from **main's** `.agent-loop.toml` (`project.gate`, or `project.gate_docs` for a docs
   tier) and, if its first word is a script in the repository, puts **main's** copy of that script in place before
   running (0022: the judged can't change the judge);
3. runs it in T's worktree and writes `gate/<H>.json`: `{head, tested, tested_tree, base, result,
   gate_command_sha256, log_sha256, command, session, finished_at}` (time by code) with a `gate.passed` or
   `gate.failed` event (failures are attributed to the PR unless main moved during the run or the command couldn't
   run or timed out);
4. checks the merge preconditions 1–6 with `verdict.check_merge` (the one function the reducer and the verify
   workflow call) and, when they hold and `merge.auto` is on, records `merge.requested` and prints the merge request:
   a `workflow_dispatch` of `agent-loop.yml` on main with the PR number, which the gate worker makes with its GitHub
   tools (loopctl only reads GitHub).

For a check unit (`check-<sha>`, the post-merge check, §10.6) it runs the same gate on main's sha itself (T = B = H)
and records `main.checked`: green or red.

`command_hash` is the hash the verify workflow recomputes from main (0024): a result made by another command or
another script is refused at merge.
"""

from __future__ import annotations

import hashlib
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopctl import clock, schema

FILE = "gate/{head}.json"
WORKFLOW = "agent-loop.yml"
TIMEOUT_S = 35 * 60  # inside the gate lease's 40 minutes
NOT_ATTRIBUTED_EXIT = (124, 126, 127)  # timed out, not executable, not found: the environment, not the PR
HASH_PREFIX = b"loopctl-gate-command-v1\0"


class GateProblem(RuntimeError):
    """The gate can't run at all (no gate command, the clone can't be fetched): no result is written."""


def script_of(command: str) -> str | None:
    """The repository script a gate command runs (its first word, when that is a relative path), else None."""
    try:
        first = shlex.split(command)[0]
    except (ValueError, IndexError):
        return None
    if first.startswith("./"):
        first = first[2:]
    if "/" not in first or first.startswith("/") or ".." in first.split("/"):
        return None
    return first


def command_hash(command: str, script: bytes | None) -> str:
    """sha256 over the gate command and the content of the script it runs (none: `-`). The verify workflow computes
    it from main's `.agent-loop.toml` and main's script; `loopctl gate` from main's copies too."""
    digest = hashlib.sha256(script).hexdigest() if script is not None else "-"
    return hashlib.sha256(HASH_PREFIX + command.encode("utf-8") + b"\0" + digest.encode("ascii")).hexdigest()


def gate_command(project_text: str | None, tier: str | None) -> str:
    """The gate command from a `.agent-loop.toml` (main's): `project.gate_docs` for a docs tier if set, else
    `project.gate`. Raises GateProblem when none is configured."""
    from loopctl.config import ConfigError, resolve
    from loopctl.risk import _get

    try:
        cfg = resolve(project_text, project_file="main:.agent-loop.toml")
    except ConfigError as exc:
        raise GateProblem(f"main's .agent-loop.toml is invalid: {exc}") from exc
    command = (_get(cfg, "project.gate_docs") if tier == "docs" else "") or _get(cfg, "project.gate")
    if not command:
        raise GateProblem("main's .agent-loop.toml sets no project.gate: there is nothing to run")
    return command


def merge_request(repo: str, main: str, prs: list[int]) -> dict[str, Any]:
    """The merge request the gate worker makes with its GitHub tools (§10.2): exactly this dispatch."""
    numbers = ",".join(str(n) for n in prs)
    return {"do": "workflow_dispatch", "repo": repo, "workflow": WORKFLOW, "ref": main, "inputs": {"prs": numbers},
            "rest": f"POST /repos/{repo}/actions/workflows/{WORKFLOW}/dispatches "
                    f'{{"ref": "{main}", "inputs": {{"prs": "{numbers}"}}}}',
            "gh": f"gh workflow run {WORKFLOW} --repo {repo} --ref {main} -f prs={numbers}"}  # fmt: skip


@dataclass(frozen=True)
class Target:
    """What to gate: a PR unit's head (kind `pr` or `revert`) or main's sha (kind `check`)."""

    kind: str  # pr | revert | check
    head: str
    pr: int | None = None
    tier: str | None = None
    revert: Mapping[str, str] | None = None  # {red, good}

    @classmethod
    def of(cls, unit: Mapping[str, Any]) -> Target:
        kind = "check" if unit["kind"] == "check" else "revert" if unit["kind"] == "revert" else "pr"
        if not unit.get("head"):
            raise GateProblem(f"{unit['key']} has no head to gate")
        return cls(kind, unit["head"], unit.get("pr"), unit.get("tier"), unit.get("revert"))


@dataclass
class Run:
    result: dict[str, Any]  # the gate/<H>.json document
    attributed: bool  # a failure that is the PR's
    main_moved: bool  # main moved while the gate ran: the result can't be merged
    log_path: Path
    note: str = ""


def _ok(io: Any, cwd: Any, *args: str) -> str:
    code, out = io.git(*args, cwd=cwd)
    if code != 0:
        raise GateProblem(f"git {args[0]} failed: {out.strip()[:300]}")
    return out.strip()


def _have(io: Any, repo: Path, sha: str) -> bool:
    return io.git("cat-file", "-e", f"{sha}^{{commit}}", cwd=repo)[0] == 0


def _fetch_commit(io: Any, repo: Path, sha: str, pr: int | None) -> None:
    if _have(io, repo, sha):
        return
    io.git("fetch", "--quiet", "origin", sha, cwd=repo)
    if not _have(io, repo, sha) and pr is not None:
        io.git("fetch", "--quiet", "origin", f"+refs/pull/{pr}/head:refs/loopctl/pr-{pr}", cwd=repo)
    if not _have(io, repo, sha):
        raise GateProblem(f"{sha[:12]} can't be fetched from the target")


def remote_heads(io: Any, repo: Path, main: str, pr: int | None) -> tuple[str | None, str | None]:
    """(main's sha now, the PR's head now) from `git ls-remote` of the target (GitHub's `refs/pull/<n>/head`)."""
    refs = [f"refs/heads/{main}"] + ([f"refs/pull/{pr}/head"] if pr is not None else [])
    code, out = io.git("ls-remote", "origin", *refs, cwd=repo)
    found = dict(reversed(line.split("\t", 1)) for line in out.splitlines() if "\t" in line) if code == 0 else {}
    return found.get(f"refs/heads/{main}"), found.get(f"refs/pull/{pr}/head") if pr is not None else None


def run(io: Any, repo: Path, target: Target, *, main: str, scratch: Path, session: str | None,
        timeout: float = TIMEOUT_S) -> Run:  # fmt: skip
    """Build T, run main's gate command on it, and return the result document (validated)."""
    repo = Path(repo)
    _ok(io, repo, "fetch", "--quiet", "origin", f"+refs/heads/{main}:refs/loopctl/main")
    base = _ok(io, repo, "rev-parse", "refs/loopctl/main^{commit}")
    head = target.head
    _fetch_commit(io, repo, head, target.pr)
    if target.kind == "check":
        base = head  # the post-merge check tests main's sha itself
    config_rev = base
    raw = io.show_bytes(f"{config_rev}:.agent-loop.toml", repo)
    command = gate_command(raw.decode("utf-8") if raw is not None else None, target.tier)
    script = script_of(command)
    script_bytes = io.show_bytes(f"{config_rev}:{script}", repo) if script else None
    chash = command_hash(command, script_bytes)

    tmp = io.tempdir()
    wt = tmp / "t"
    notes: list[str] = []
    pre_log = b""
    failed_before_run = False
    try:
        _ok(io, repo, "worktree", "add", "--detach", "--force", str(wt), base)
        if target.kind == "check":
            tested = head
        else:
            code, out = io.git("merge", "--squash", head, cwd=wt)
            if code != 0:
                failed_before_run = True
                pre_log = f"[loopctl gate] {head[:12]} doesn't merge cleanly onto main {base[:12]}:\n{out}\n".encode()
            else:
                _ok(io, wt, "commit", "--quiet", "--no-verify", "--allow-empty", "-m",
                    f"loopctl gate: {head[:12]} squashed onto {base[:12]}")  # fmt: skip
            tested = _ok(io, wt, "rev-parse", "HEAD")
            if target.kind == "revert" and not failed_before_run:
                rv = target.revert or {}
                _fetch_commit(io, repo, rv.get("good", ""), None)
                exact = (_ok(io, repo, "rev-parse", f"{head}^{{tree}}") == _ok(io, repo, "rev-parse", f"{rv['good']}^{{tree}}")
                         and _ok(io, repo, "rev-parse", f"{head}^") == rv["red"])  # fmt: skip
                if not exact:
                    failed_before_run = True
                    pre_log = (f"[loopctl gate] {head[:12]} is not an exact revert: its parent must be {rv['red'][:12]}"
                               f" and its tree the tree of {rv['good'][:12]}\n").encode()  # fmt: skip
        tree = _ok(io, wt, "rev-parse", "HEAD^{tree}")
        if failed_before_run:
            code, log = 1, pre_log
        else:
            if script and script_bytes is not None:
                path = wt / script
                current = path.read_bytes() if path.exists() else None
                if current != script_bytes:
                    io.write_file(path, script_bytes, 0o755)
                    notes.append(f"{script} restored from main before the run (0022)")
                    pre_log = f"[loopctl gate] {script} differs from main's: main's copy runs\n".encode()
            code, log = io.run_gate(command, wt, timeout)
            log = pre_log + log
    finally:
        io.git("worktree", "remove", "--force", str(wt), cwd=repo)
        io.remove(tmp)
    log_path = Path(scratch) / f"gate-{head}.log"
    io.write_file(log_path, log)
    moved = False
    if target.kind != "check":
        now_main, _ = remote_heads(io, repo, main, None)
        moved = now_main is not None and now_main != base
    result = {"head": head, "tested": tested, "tested_tree": tree, "base": base,
              "result": "pass" if code == 0 else "fail", "gate_command_sha256": chash,
              "log_sha256": hashlib.sha256(log).hexdigest(), "command": command[:500], "session": session,
              "finished_at": clock.stamp()}  # fmt: skip
    schema.validate(result, "gate_result")
    attributed = code != 0 and not moved and code not in NOT_ATTRIBUTED_EXIT
    if moved:
        notes.append(f"main moved from {base[:12]} while the gate ran")
    return Run(result, attributed, moved, log_path, "; ".join(notes))


def main_green_for(state: Mapping[str, Any], unit: Mapping[str, Any]) -> bool:
    """Precondition 5 as `loopctl gate` sees it: main is not known red (its post-merge check of the newest sha may
    still be running: if it fails, that check reverts). The revert PR that makes a red main green is exempt."""
    return unit.get("kind") == "revert" or state["main"]["green"] is not False
