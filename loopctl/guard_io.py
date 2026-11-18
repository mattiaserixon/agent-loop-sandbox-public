"""The guard's only I/O: local git reads, the `STOP` fetch, and the per-session state file.

Kept apart from the policy (`loopctl/guard.py`, pure apart from the injected `git`) so the read-only lint
(tests/test_github_readonly.py) can allow exactly this module to run processes. Its git runner refuses every
subcommand that writes to a remote: the guard never pushes anything (invariant I11).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# What the guard's git may run. `fetch` only into the guard's own cache repository (`stop_reader`); the rest read the
# checkout a command runs in, to see where a push would go. `init` creates that cache repository.
GIT_ALLOWED = frozenset({"remote", "rev-parse", "config", "symbolic-ref", "fetch", "ls-tree", "show", "init"})
# A background fsmonitor daemon started by git inherits our output pipe and keeps it open, so the caller waits for its
# timeout. These git calls never need one.
GIT_NO_DAEMON = ("-c", "core.fsmonitor=false")


class GitFailed(Exception):
    """A git call the guard needed failed: the decision that needed it fails closed."""


def quiet_git_env() -> dict[str, str]:
    """The environment for git when nobody can answer it: no credential or SSH prompts, no optional locks."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def run_git(*args: str, cwd: str | None = None, timeout: float = 20) -> str:
    """`git <args>` in cwd; its output, or GitFailed. Only the subcommands in GIT_ALLOWED run."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    sub = args[i] if i < len(args) else ""
    if sub not in GIT_ALLOWED or "push" in args:
        raise GitFailed(f"the guard doesn't run `git {sub}`")
    try:
        out = subprocess.run(  # noqa: S603
            ["git", *GIT_NO_DAEMON, *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            env=quiet_git_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFailed(f"git {' '.join(args[:3])}: {exc}") from exc
    if out.returncode != 0:
        raise GitFailed(f"git {' '.join(args[:3])}: {out.stderr.strip()[:200]}")
    return out.stdout


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "agent-loop" / "guard"


class FileStore:
    """One JSON file per session under the cache directory. A file that can't be read is an empty session; a write
    that fails is ignored (the next call reads again, which only costs a fetch)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:120] or "unknown"
        return self.directory / f"session-{safe}.json"

    def load(self, session_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, session_id: str, data: dict[str, Any]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".session-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, self._path(session_id))
        except OSError:
            pass


def claim_refused_reader(env: dict[str, str] | None = None) -> Callable[[], str | None]:
    """A reader for guard_stop.Gate: why this container's session lost its lease claim (claim.REFUSED_FILE, written
    by loopctl), or None."""
    from loopctl import claim

    path = claim.home(env) / claim.REFUSED_FILE

    def read() -> str | None:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError):
            return "the lease claim's refusal marker can't be read"  # fail closed
        return text or "another session claimed this unit's lease"

    return read


def state_url(state: str, state_dir: str | None, git: Callable[..., str] = run_git) -> str:
    """Where to fetch the state repo from: the attached clone's own remote (in a cloud session that is the platform's
    git access), else GitHub."""
    if state_dir:
        try:
            url = git("remote", "get-url", "origin", cwd=state_dir).strip()
            if url:
                return url
        except GitFailed:
            pass
    return f"https://github.com/{state}.git"


def stop_reader(url: str, cache_dir: Path, branch: str = "main",
                git: Callable[..., str] = run_git) -> Callable[[float], str]:  # fmt: skip
    """A reader for guard_stop.Gate: `git fetch --depth 1` of the state repo's main into the guard's own bare cache
    repository, then `STOP` from it ("" when main has no STOP). Never touches the worker's checkout."""

    def read(timeout: float) -> str:
        deadline = time.monotonic() + timeout

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitFailed(f"reading STOP took more than {timeout:.0f} s")
            return remaining

        repo = cache_dir / "stop.git"
        if not (repo / "HEAD").exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            git("init", "-q", "--bare", str(repo), timeout=left())
        ref = "refs/agent-loop/stop"
        git("fetch", "-q", "--depth", "1", "--no-tags", url, f"+refs/heads/{branch}:{ref}", cwd=str(repo), timeout=left())
        listing = git("ls-tree", "--name-only", ref, "--", "STOP", cwd=str(repo), timeout=left())
        if "STOP" not in listing.split():
            return ""
        return git("show", f"{ref}:STOP", cwd=str(repo), timeout=left())

    return read
