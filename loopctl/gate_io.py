"""`loopctl gate`'s only I/O: local git in the target's clone, and the project's gate command (phase2-design §9.4).

Kept apart from the logic (`loopctl/gate.py`) so the read-only lint (tests/test_github_readonly.py) can allow exactly
this module to run processes. Its git runner refuses every subcommand that writes to a remote: the gate fetches the
target, builds T in a scratch worktree and runs the gate there; it never pushes anything (invariant I11). The
results go to the state repo only through `loopctl/store.py`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# What the gate's git may run, all local but `fetch` and `ls-remote` (reads of the target).
GIT_ALLOWED = frozenset({"fetch", "ls-remote", "rev-parse", "cat-file", "show", "worktree", "merge", "commit",
                         "diff", "status", "checkout"})  # fmt: skip
IDENTITY = ("loopctl gate", "loopctl@users.noreply.github.com")
LOG_MAX = 50_000_000  # bytes of gate output kept (and hashed); the rest is cut with a marker


class GitRefused(PermissionError):
    """The gate tried a git subcommand outside its allowlist."""


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_AUTHOR_NAME=IDENTITY[0],
               GIT_AUTHOR_EMAIL=IDENTITY[1], GIT_COMMITTER_NAME=IDENTITY[0], GIT_COMMITTER_EMAIL=IDENTITY[1])  # fmt: skip
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def require_allowed(args: tuple[str, ...] | list[str]) -> None:
    sub = args[0] if args else ""
    if sub not in GIT_ALLOWED or "push" in args or any(a.startswith(("--upload-pack", "--exec")) for a in args):
        raise GitRefused(f"loopctl gate doesn't run `git {sub}`")


@dataclass
class GateIO:
    """The real I/O. Tests pass a repository on disk; nothing here is faked."""

    def git(self, *args: str, cwd: str | Path, timeout: float = 600) -> tuple[int, str]:
        require_allowed(args)
        r = subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=str(cwd), capture_output=True,  # noqa: S603, S607
                           text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL, env=_env())  # fmt: skip
        return r.returncode, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout))

    def show_bytes(self, rev_path: str, cwd: str | Path) -> bytes | None:
        """`git show <rev>:<path>` as bytes, or None if there is no such file."""
        r = subprocess.run(["git", "show", rev_path], cwd=str(cwd), capture_output=True, timeout=60, check=False,  # noqa: S603, S607
                           stdin=subprocess.DEVNULL, env=_env())  # fmt: skip
        return r.stdout if r.returncode == 0 else None

    def run_gate(self, command: str, cwd: str | Path, timeout: float) -> tuple[int, bytes]:
        """The project's gate command in a shell, stdout and stderr together. 124 = timed out."""
        try:
            r = subprocess.run(["sh", "-c", command], cwd=str(cwd), stdout=subprocess.PIPE,  # noqa: S603, S607
                               stderr=subprocess.STDOUT, timeout=timeout, check=False, stdin=subprocess.DEVNULL)  # fmt: skip
            code, log = r.returncode, r.stdout
        except subprocess.TimeoutExpired as exc:
            code, log = 124, (exc.stdout or b"") + f"\n[loopctl gate: timed out after {timeout:.0f} s]\n".encode()
        if len(log) > LOG_MAX:
            log = log[:LOG_MAX] + b"\n[loopctl gate: output cut here]\n"
        return code, log

    def write_file(self, path: Path, data: bytes, mode: int | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mode is not None:
            os.chmod(path, mode)

    def tempdir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="loopctl-gate-"))

    def remove(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)
