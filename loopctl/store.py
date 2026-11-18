"""The state repo: layout, event files and compare-and-swap writes (phase2-design §5, decision record 0003).

Layout of the state repo's `main` (§5.1):

    state.json                                   current state; one writer, the controller under its lease
    events/YYYY/MM/DD/<ulid>-<writer>.json       one immutable file per event (writer `worker:fix` -> `worker-fix`)
    notes.json                                   low-priority notes (M0.4)
    gate/<head>.json                             gate results (M0.10)
    claims/<lease_token_sha256>.json             which session claimed a unit lease (first call wins, 0029)
    STOP                                         stop lines (M0.7, decision record 0026)
    archive/                                     monthly archives (cut until day 60, build-plan §6)

Writes are compare-and-swap by rejected push (spike M0.1 c): fetch, reset to the remote's `main`, decide from what
is there, commit, push. A rejected push means another writer moved first: fetch again and decide again. A lease
exists only if the push that granted it won (§5.3).

This module and `github.py` are the only engine modules that run git. `github.py` only reads the target; this
module writes only to the state repo, whose remote URL is fixed when the `Store` is made and checked before every
push (proof test T-INV-11 a).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loopctl import clock, schema

STATE_FILE = "state.json"
EVENTS_DIR = "events"
GATE_DIR = "gate"
CLAIMS_DIR = "claims"  # first-call claims of unit leases (loopctl/claim.py): written by workers, create-only
BRANCH = "main"
COMMITTER = ("loopctl", "loopctl@users.noreply.github.com")

EMPTY_STATE: dict[str, Any] = {
    "version": 1,
    "controller": None,
    "previous_controller": None,
    "units": {},
    "main": {"sha": None, "green": None},
    "config_hash": None,
    "questions": {},
    "last_tick_at": None,
    "applied": [],
    "spend": [],
    "sessions": {},
    "cost_samples": {},
    "signals": {},
}


class StoreError(RuntimeError):
    """The state repo can't be read or written as asked."""


class CASExhausted(StoreError):
    """Every push attempt was rejected because other writers kept moving first."""


class Fenced(StoreError):
    """The writer doesn't hold the current controller lease (invariant I10): nothing was written."""


def empty_state() -> dict[str, Any]:
    return copy.deepcopy(EMPTY_STATE)


# ------------------------------------------------------------------------------------------------------------ events
def cid(unit: str | None, attempt: int = 1) -> str:
    return f"{unit}#{attempt}" if unit else "controller"


def make_event(type_: str, payload: dict[str, Any], *, writer: str, unit: str | None = None,
               epoch: int | None = None, lease_token: str | None = None, attempt: int = 1,
               claimant: str | None = None) -> dict[str, Any]:  # fmt: skip
    """A new event with its id and time written by the clock (I5). Validated before it is returned. `claimant` is
    the sha256 of the worker session that claimed the lease (decision record 0029, claim.py)."""
    event = {"id": clock.ulid(), "type": type_, "unit": unit, "cid": cid(unit, attempt), "writer": writer,
             "epoch": epoch, "at": clock.stamp(), "payload": payload}  # fmt: skip
    if lease_token is not None:
        event["lease_token"] = lease_token
    if claimant is not None:
        event["claimant_sha256"] = claimant
    schema.validate_event(event)
    return event


def event_path(event: dict[str, Any]) -> str:
    """events/YYYY/MM/DD/<ulid>-<writer>.json, the day taken from the event's own stamp."""
    day = event["at"][:10].replace("-", "/")
    return f"{EVENTS_DIR}/{day}/{event['id']}-{event['writer'].replace(':', '-')}.json"


def dumps(doc: Any) -> str:
    return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def read_event_files(root: Path, days: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """Event files of a state repo clone at `root` (all, or only those under the given `YYYY/MM/DD` days), sorted by
    id. Files that aren't valid events are returned as `{"id": <file stem id>, "invalid": reason}` so the reducer can
    reject them."""
    base = Path(root) / EVENTS_DIR
    roots = [base / d for d in days] if days is not None else [base]
    out = []
    for folder in roots:
        if not folder.exists():
            continue
        for file in sorted(folder.rglob("*.json")):
            try:
                out.append(json.loads(file.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                out.append({"id": file.name.split("-")[0], "invalid": f"{file.name}: {exc}"})
    return sorted(out, key=lambda e: str(e.get("id", "")))


def recent_event_files(root: Path) -> list[dict[str, Any]]:
    """The event files of the last GLOBAL_APPLIED_DAYS days: the ones a reader folds into `state.json` (the reducer
    skips those already applied, I4; older unit-less ids are pruned from `applied`, so older files aren't read)."""
    from datetime import timedelta

    from loopctl.reducer import GLOBAL_APPLIED_DAYS

    now = clock.now()
    return read_event_files(root, [(now - timedelta(days=d)).strftime("%Y/%m/%d") for d in range(GLOBAL_APPLIED_DAYS)])


# --------------------------------------------------------------------------------------------------------------- git
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_AUTHOR_NAME=COMMITTER[0], GIT_AUTHOR_EMAIL=COMMITTER[1],
               GIT_COMMITTER_NAME=COMMITTER[0], GIT_COMMITTER_EMAIL=COMMITTER[1])  # fmt: skip
    return env


@dataclass
class Store:
    """A clone of the state repo at `path` whose `origin` must be `remote` (an URL or a local path in tests)."""

    path: Path
    remote: str
    runner: Runner = subprocess.run
    retries: int = 6
    pushed: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    # -- plumbing
    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self.runner(["git", *args], cwd=self.path, capture_output=True, text=True, timeout=120,
                             check=False, env=_git_env())  # fmt: skip
        if check and result.returncode != 0:
            raise StoreError(f"git {args[0]} failed: {(result.stderr or result.stdout).strip()[:500]}")
        return result

    def _check_remote(self) -> None:
        url = self._git("remote", "get-url", "origin").stdout.strip()
        if url != self.remote:
            raise StoreError(f"the state clone's origin is {url!r}, not the configured state repo {self.remote!r}")

    @staticmethod
    def configured_origin(path: Path, runner: Runner = subprocess.run) -> tuple[str, str]:
        """(origin URL as configured, origin URL as git uses it after any url rewriting) of a clone."""
        def git(*args: str) -> str:
            return runner(["git", *args], cwd=path, capture_output=True, text=True, timeout=30, check=False,
                          env=_git_env()).stdout.strip()  # fmt: skip

        return git("config", "--get", "remote.origin.url"), git("remote", "get-url", "origin")

    @classmethod
    def clone(cls, remote: str, path: Path, runner: Runner = subprocess.run) -> Store:
        result = runner(["git", "clone", "--quiet", remote, str(path)], capture_output=True, text=True, timeout=120,
                        check=False, env=_git_env())  # fmt: skip
        if result.returncode != 0:
            raise StoreError(f"git clone failed: {result.stderr.strip()[:500]}")
        return cls(Path(path), remote, runner)

    def sync(self) -> str | None:
        """Fetch and reset to the remote's main. Returns its sha (None while the repo is empty)."""
        self._check_remote()
        fetched = self._git("fetch", "--quiet", "origin", check=False)
        if fetched.returncode != 0 and "couldn't find remote ref" not in fetched.stderr:
            raise StoreError(f"git fetch failed: {fetched.stderr.strip()[:500]}")
        head = self._git("rev-parse", "--verify", "--quiet", f"origin/{BRANCH}", check=False)
        if head.returncode != 0:
            return None
        self._git("checkout", "--quiet", "-B", BRANCH, f"origin/{BRANCH}")
        self._git("reset", "--quiet", "--hard", f"origin/{BRANCH}")
        self._git("clean", "--quiet", "-fdx")
        return head.stdout.strip()

    # -- reads (from the synced working copy)
    def read_state(self) -> dict[str, Any]:
        file = self.path / STATE_FILE
        if not file.exists():
            return empty_state()
        state = json.loads(file.read_text(encoding="utf-8"))
        schema.validate(state, "state")
        return state

    def read_file(self, rel: str) -> str | None:
        file = self.path / rel
        return file.read_text(encoding="utf-8") if file.exists() else None

    def read_events(self, days: Iterable[str] | None = None) -> list[dict[str, Any]]:
        return read_event_files(self.path, days)

    # -- writes
    def _commit_push(self, files: dict[str, str], message: str) -> bool:
        for rel, text in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self._git("add", "--", *files)
        if not self._git("status", "--porcelain").stdout.strip():
            return True  # nothing changed: nothing to push
        self._git("commit", "--quiet", "-m", message)
        self._check_remote()
        pushed = self._git("push", "--quiet", "origin", f"HEAD:refs/heads/{BRANCH}", check=False)
        if pushed.returncode == 0:
            self.pushed += 1
            return True
        text = pushed.stderr
        if any(m in text for m in ("rejected", "fetch first", "non-fast-forward", "cannot lock ref", "failed to push")):
            return False
        raise StoreError(f"git push failed: {text.strip()[:500]}")

    def transact(self, decide: Callable[[dict[str, Any], Store], Write | None], message: str) -> Write | None:
        """The CAS loop: sync, `decide(state, store)` -> a Write (or None for nothing to write), commit, push; on a
        rejected push sync and decide again. Returns the Write that won."""
        for _ in range(self.retries):
            self.sync()
            write = decide(self.read_state(), self)
            if write is None:
                return None
            files = dict(write.files)
            if write.state is not None:
                schema.validate(write.state, "state")
                files[STATE_FILE] = dumps(write.state)
            for event in write.events:
                schema.validate_event(event)
                files[event_path(event)] = dumps(event)
            if self._commit_push(files, message):
                return write
        raise CASExhausted(f"{self.retries} pushes to the state repo were rejected; another writer kept moving first")

    def controller_transact(self, session: str, epoch: int | None,
                            decide: Callable[[dict[str, Any], Store], Write | None], message: str) -> Write | None:  # fmt: skip
        """A controller write, fenced (I10): it is decided only while the store's controller lease is held by
        `session` at `epoch`. `epoch=None` is the lease-taking write, whose own check is in `leases`."""

        def fenced(state: dict[str, Any], store: Store) -> Write | None:
            if epoch is not None:
                lease = state.get("controller")
                if not lease or lease["session"] != session or lease["epoch"] != epoch:
                    held = f"{lease['session']} at epoch {lease['epoch']}" if lease else "nobody"
                    raise Fenced(f"controller lease is held by {held}, not {session} at epoch {epoch}")
            return decide(state, store)

        return self.transact(fenced, message)

    def append_events(self, events: Sequence[dict[str, Any]], message: str,
                      check: Callable[[dict[str, Any]], dict[str, str] | None] | None = None,
                      files: dict[str, str] | None = None) -> None:  # fmt: skip
        """A worker's write: new event files, a gate result under `gate/` and a lease claim under `claims/` (never
        state.json). `check(state)` runs against the freshly synced state (and this clone's files) before every
        attempt (the lease-token and claim checks, decision record 0029) and raises to refuse; it may return more
        files to write in the same commit (the claim, on the first call), so the claim and the events win or lose
        one compare-and-swap together."""

        def allowed(path: str) -> bool:
            return path.startswith((GATE_DIR + "/", CLAIMS_DIR + "/")) and ".." not in path.split("/")

        bad = [p for p in (files or {}) if not allowed(p)]
        if bad:
            raise StoreError(f"a worker writes only event files, {GATE_DIR}/ results and {CLAIMS_DIR}/, not {bad}")

        def decide(state: dict[str, Any], _store: Store) -> Write | None:
            extra = check(state) if check is not None else None
            if extra and any(not p.startswith(CLAIMS_DIR + "/") or ".." in p.split("/") for p in extra):
                raise StoreError(f"a worker's check may add only {CLAIMS_DIR}/ files, not {sorted(extra)}")
            out = {**(files or {}), **(extra or {})}
            if not events and not out:
                return None  # nothing to write (a claim already held)
            return Write(events=list(events), files=out)

        self.transact(decide, message)


@dataclass
class Write:
    """What one CAS commit writes: the new state (controller only), event files, other files (path -> text)."""

    state: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    result: Any = None
