"""`loopctl guard`: the PreToolUse hook of every worker and of the controller (phase2-design §2, §4.5, §12.4;
decision records 0026 and 0028; proof tests T-GUARD and T-INV-11 e).

The environment's setup script installs it in the user settings (M0.13), with the role and the repositories:

    loopctl guard --role worker --target owner/target --state owner/state --state-dir auto \
        --engine-repo owner/agent-loop --engine-dir auto

Each option falls back to an environment variable (`AGENT_LOOP_ROLE`, `AGENT_LOOP_TARGET`, `AGENT_LOOP_STATE`,
`AGENT_LOOP_STATE_DIR`, `AGENT_LOOP_MAIN`, `AGENT_LOOP_ENGINE_DIR`, `AGENT_LOOP_ENGINE_REPO`, `AGENT_LOOP_GUARD_CACHE`).
`--state-dir auto` and `--engine-dir auto` (what the setup scripts install, `loopctl/install.py`) find the attached
checkout whose origin is the state or engine repository. With no role it acts as
a worker (the stricter role for merges and session tools). The hook reads the event on stdin; exit 2 with the reason
on stderr refuses the call. It matches every tool (the `STOP` re-read applies to any tool call).

**Both roles refuse:** every merge (`merge_pull_request` and auto-merge tools of any MCP server, `gh pr merge`, the
REST merge endpoint, GraphQL `mergePullRequest`/auto-merge): merges happen only in the verify workflow after a
`loopctl merge-request`; API writes that move a branch without a merge (`merges`, `git/refs`, `git/commits`,
`contents/`, branch renames, `updateRef` …); writes of Actions secrets and variables.

**Worker role** (fix, review, gate, triage sessions):
- no `git push` to `main` of the target (any spelling, force or not: changes reach main only through the workflow's
  merge), no push to the state repo outside `loopctl` (the store has one writer), no push to any other repository;
- GitHub writes (MCP write tools, `gh` write commands, non-`GET` `gh api` and HTTP to GitHub) go only to the target;
  the GitHub file tools never write to `main`;
- `create_session`, the remote-trigger tools (`create/update/delete/fire_trigger`, `send_later`, `RemoteTrigger`),
  session control (`interrupt/archive_session`) and `ArtifactData` (the page's database: an agent could forge an
  owner command) are refused (§12.4);
- reads of the state repo and of the engine's docs are refused (§8.2: workers read only their brief);
- the `STOP` gate (loopctl/guard_stop.py): irreversible steps (pushes, GitHub writes, `loopctl merge-request`) need
  a `STOP` read at most 60 s old; any call re-reads after 120 s; once stopped only `loopctl finish … --stopped` runs;
- a session whose `loopctl` call was refused because another session claimed the unit's lease (a duplicate start,
  `loopctl/claim.py`) is stopped the same way.

**Controller role** (owner condition 2 of decision record 0028, invariant I11): the controller only reads GitHub and
writes only to the state repo, through `loopctl`. So every `git push` typed in a command is refused (`loopctl`'s own
push to the state repo runs inside `loopctl`, which the hook doesn't see as a push), every non-`GET` `gh api` or HTTP
request to GitHub, every `gh` command that isn't a read, and every GitHub MCP write tool. GraphQL queries are reads;
any mutation is refused. Session tools stay allowed: the controller starts workers.

Commands are read the way the shell runs them (carried from v1's `scripts/merge-guard.py` with its tests): wrappers
(`env`, `timeout`, `sudo`, `bash -lc` …), `…` and $(…) spans, and heredocs fed to a shell are checked too. Anything
the guard can't read that looks like a push or a merge is refused. Any error while deciding refuses the call (fail
closed). Known limit, as in v1: a program that writes by other means (a Python script opening a socket) isn't seen;
the guard makes the rules hard to break by accident, and GitHub's ruleset and the App-only merge make them hard to
break on purpose.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loopctl import clock
from loopctl.guard_stop import Gate

ROLES = ("worker", "controller")
Git = Callable[..., str]


class Unparsed(ValueError):
    """A shell command shlex can't split."""


class CheckFailed(Exception):
    """Something needed for the decision couldn't be read: the call is refused (fail closed)."""


@dataclass(frozen=True)
class Policy:
    """What the guard protects. Paths are absolute directories of checkouts in the session's machine."""

    role: str = "worker"
    target: str = ""  # owner/name of the target repository
    state: str = ""  # owner/name of the state repository
    main: str = "main"  # the target's main branch
    state_dir: str | None = None  # the state repo's checkout, if attached
    engine_dir: str | None = None  # agent-loop's checkout (its docs/ are off limits to workers)

    @property
    def controller(self) -> bool:
        return self.role == "controller"

    def protected(self) -> list[str]:
        dirs = [self.state_dir] if self.state_dir else []
        if self.engine_dir:
            dirs.append(os.path.join(self.engine_dir, "docs"))
        return [os.path.normpath(d) for d in dirs]


def same_repo(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a.lower() == b.lower()


def repo_of_url(url: str) -> str | None:
    """`owner/name` of a git remote URL or path (https, ssh, scp-like, a proxy path), else None."""
    tail = re.split(r"[:/]", url.strip().rstrip("/").removesuffix(".git"))
    tail = [part for part in tail if part]
    return f"{tail[-2]}/{tail[-1]}" if len(tail) >= 2 else None


# --- messages ---------------------------------------------------------------------------------------------------

MERGE_REFUSED = (
    "agents never merge: a PR reaches main only through `loopctl merge-request` and the loop's verify workflow, "
    "which merges as the loop's App after its checks; merge tools, `gh pr merge`, the REST/GraphQL merge calls and "
    "auto-merge are refused"
)
API_WRITE = (
    "the guard refuses API calls that merge a PR or write a branch directly (merges, git refs and commits, contents, "
    "branch renames): change code on a branch, push it, and let the verify workflow merge"
)
SECRETS = "Actions secrets and variables are the owner's alone: writing them is refused (invariant I11)"
STATE_WRITE = (
    "no writes to the state repo outside `loopctl`: the store has one writer, and agents use the `loopctl` commands "
    "in their brief (phase2-design §5, §12.4)"
)
WORKER_READS = (
    "workers read only their brief: the state repo and the engine's docs are off limits (phase2-design §8.2)"
)
WORKER_SESSION_TOOL = (
    "{tool} is refused in a worker: only the controller starts sessions and tasks, and the page's database is the "
    "owner's channel (phase2-design §4.4, §12.4)"
)
CONTROLLER_PUSH = (
    "the controller writes only to the state repo, and only through `loopctl`: `git push` is refused here "
    "(invariant I11, decision record 0028)"
)
CONTROLLER_WRITE = (
    "the controller only reads GitHub (invariant I11, decision record 0028): {what} is refused; it writes only to the "
    "state repo, through `loopctl`"
)
UNREADABLE = "the guard can't read this command and it looks like a push or a merge: split it up"


def no_main(main: str) -> str:
    return (f"never push to {main}: changes reach {main} only through a PR that the loop's verify workflow merges "
            "(never force-push or delete it either)")  # fmt: skip


def only_target(target: str, what: str) -> str:
    where = f"the target {target}" if target else "the target (none is configured, so nothing can be written)"
    return f"workers write only to {where}: {what} is refused"


# --- shell parsing (carried from v1 merge-guard) -----------------------------------------------------------------

GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
FORCE_LONG = ("--force", "--force-with-lease", "--force-if-includes")
ALL_BRANCHES = ("--mirror", "--all", "--branches")
SHELLS = {"bash", "sh", "zsh", "dash"}
SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
HTTP_CLIENTS = {"curl", "wget", "http", "xh"}
STARTS = {"git", "gh", "eval", "env", "loopctl", *SHELLS, *HTTP_CLIENTS}
SHELL_OPTS_WITH_VALUE = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
# Words a command must contain to need parsing for pushes, merges and GitHub writes; anything else is only checked for
# paths (worker) and returns at once.
ACTION_WORDS = re.compile(
    r"push|merge|Merge|/git/|contents/|Ref|CommitOnBranch|rename|graphql|mutation|\bgh\b|curl|wget|\bhttp\b|\bxh\b"
    r"|loopctl|github|secrets|variables|send-pack"
)
REST_MERGE = re.compile(r"/pulls/[^/\s]+/merge\b")
GRAPHQL_MERGE = re.compile(r"\b(mergePullRequest|enablePullRequestAutoMerge)\b")
ROUGH_PUSH = re.compile(r"\bgit\b[^\n;&|]*\b(push|send-pack)\b")
ROUGH_MERGE = re.compile(r"\bgh\s+pr\s+merge\b|/pulls/[^/\s]+/merge\b|\bmergePullRequest\b")
# REST paths whose writes merge a PR or move, rename or rewrite a branch (main among them) without a PR merge.
API_WRITE_PATHS = re.compile(
    r"/pulls/[^/\s]+/merge\b|/merges\b|/merge-upstream\b|/git/refs\b|/git/commits\b|/contents/"
    r"|/branches/[^/\s]+/rename\b"
)
SECRET_PATHS = re.compile(r"/actions/(secrets|variables)\b|/environments/[^/\s]+/(secrets|variables)\b")
# GraphQL mutations that merge or write a branch.
GRAPHQL_WRITES = re.compile(
    r"\b(mergePullRequest|enablePullRequestAutoMerge|mergeBranch|updateRefs?|deleteRef|createCommitOnBranch)\b"
)
GRAPHQL_MUTATION = re.compile(r"\bmutation\b")
GH_API_OPTS_WITH_VALUE = {
    "-X", "--method", "-f", "--raw-field", "-F", "--field", "-H", "--header", "--input", "-q", "--jq",
    "-t", "--template", "--hostname", "--cache", "-p", "--preview",
}  # fmt: skip
GH_API_FIELDS = {"-f", "--raw-field", "-F", "--field"}
REPO_PATH = re.compile(r"(?:^|/)repos/([^/\s?#]+)/([^/\s?#]+)")
GITHUB_HOST = re.compile(r"(^|[/@.])github\.com\b|githubusercontent\.com\b", re.IGNORECASE)
# gh commands that only read; every other gh command is a write. None: every subcommand reads.
GH_READS: dict[str, set[str] | None] = {
    "pr": {"view", "list", "diff", "checks", "status"},
    "issue": {"view", "list", "status"},
    "repo": {"view", "list", "clone"},
    "run": {"view", "list", "watch", "download"},
    "workflow": {"view", "list"},
    "release": {"view", "list", "download"},
    "label": {"list"},
    "gist": {"view", "list"},
    "cache": {"list"},
    "ruleset": {"view", "list", "check"},
    "auth": {"status"},
    "secret": {"list"},
    "variable": {"list", "get"},
    "search": None, "browse": None, "status": None, "help": None, "version": None, "completion": None,
}  # fmt: skip
GH_OPTS_WITH_VALUE = {"-R", "--repo"}
CURL_WRITE_OPTS = ("-d", "--data", "-F", "--form", "-T", "--upload-file", "--json")
WGET_WRITE_OPTS = ("--post-data", "--post-file", "--body-data", "--body-file")
HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(\\?)(['\"]?)([A-Za-z0-9_.-]+)\3")


def commands(text: str) -> list[list[str]]:
    lexer = shlex.shlex(text.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    current: list[str] = []
    out: list[list[str]] = []
    try:
        for token in lexer:
            if token in SEPARATORS or set(token) <= set(";&|()"):
                if current:
                    out.append(current)
                current = []
            else:
                current.append(token)
    except ValueError as exc:  # unbalanced quotes (a heredoc with an apostrophe, say): see Walk.shell
        raise Unparsed(str(exc)) from exc
    if current:
        out.append(current)
    return out


def branch_of(ref: str) -> str | None:
    """The branch a push destination names, the way git reads it: `main`, `heads/main` and `refs/heads/main` are all
    main; another `refs/…` (a tag, a note) is no branch."""
    if ref.startswith("refs/heads/"):
        return ref.removeprefix("refs/heads/")
    if ref.startswith("refs/"):
        return None
    return ref.removeprefix("heads/")


def program(token: str) -> str:
    """The program a word names: `git`, `/usr/bin/git` and `./git` all name git."""
    name = token.rsplit("/", 1)[-1] if token.startswith(("/", "./", "../")) else token
    return name.removesuffix(".exe")


def substitutions(text: str) -> list[str]:
    """The text of every `…` and $(…) / <(…) / >(…) span, quoted or not (quotes don't stop them running)."""
    spans: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "`":
            end = text.find("`", i + 1)
            end = len(text) if end < 0 else end
            spans.append(text[i + 1 : end])
            i = end + 1
        elif text[i] in "$<>" and text[i + 1 : i + 2] == "(":
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            spans.append(text[i + 2 : j - 1 if not depth else j])
            i = j
        else:
            i += 1
    return spans


@dataclass
class Heredoc:
    body: str
    quoted: bool  # <<'EOF', <<"EOF", <<\EOF: nothing in the body is expanded
    to_shell: bool  # the command reading it is a shell (`bash <<EOF`), so the body is a script


def inside_quotes(prefix: str) -> bool:
    """Whether the end of `prefix` (one line) is inside a '…' or "…" string."""
    quote = None
    escaped = False
    for char in prefix:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote is None and char in "'\"":
            quote = char
        elif char == quote:
            quote = None
    return quote is not None


def split_heredocs(text: str) -> tuple[str, list[Heredoc]]:
    """The command text without heredoc bodies, and the bodies. A body is data, not commands, unless a shell reads
    it; an unquoted one still runs its `…` and $(…)."""
    lines = text.split("\n")
    kept: list[str] = []
    docs: list[Heredoc] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for match in HEREDOC.finditer(line):
            if inside_quotes(line[: match.start()]):
                continue
            before = re.split(r"[;&|()]", line[: match.start()])[-1].split()
            to_shell = any(program(word) in SHELLS for word in before) and not any(
                word.startswith("-") and not word.startswith("--") and "c" in word for word in before
            )
            strip_tabs, delimiter = match.group(1) == "-", match.group(4)
            end = next(
                (k for k in range(i, len(lines)) if (lines[k].lstrip("\t") if strip_tabs else lines[k]) == delimiter),
                None,
            )
            if end is None:  # no closing line: not read as a heredoc, so the rest stays commands
                continue
            body, i = lines[i:end], end + 1
            docs.append(Heredoc("\n".join(body), bool(match.group(2) or match.group(3)), to_shell))
    return "\n".join(kept), docs


def shell_string(argv: list[str]) -> str | None:
    """The script a shell runs with -c (also in a cluster such as -lc), else None."""
    has_c, i = False, 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith(("-", "+")) or arg in ("-", "+"):
            break
        if arg in SHELL_OPTS_WITH_VALUE:
            i += 2
            continue
        has_c = has_c or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:])
        i += 1
    if not has_c:
        return None
    return argv[i] if i < len(argv) else ""


def env_split_string(argv: list[str]) -> str | None:
    """`env -S 'cmd args'` / `--split-string=…` runs that string as a command."""
    for i, arg in enumerate(argv[1:], 1):
        if arg in ("-S", "--split-string") and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--split-string="):
            return arg.split("=", 1)[1]
        if arg.startswith("-S") and len(arg) > 2:
            return arg[2:]
    return None


def in_dir(cwd: str | None, path: str) -> str:
    return path if path.startswith("/") or cwd is None else f"{cwd}/{path}"


def env_dir(prefix: list[str], cwd: str | None) -> str | None:
    """`env -C dir git …` / `env --chdir=dir git …` runs git in dir."""
    for i, arg in enumerate(prefix):
        if arg in ("-C", "--chdir") and i + 1 < len(prefix):
            cwd = in_dir(cwd, prefix[i + 1])
        elif arg.startswith("--chdir="):
            cwd = in_dir(cwd, arg.split("=", 1)[1])
    return cwd


def strict_loopctl(text: str) -> list[str] | None:
    """The argv of a command that is exactly one plain `loopctl …` call (no substitution, redirection, chaining or
    variable), else None."""
    if any(c in text for c in "$`<>;&|()\n\\"):
        return None
    try:
        argv = shlex.split(text)
    except ValueError:
        return None
    return argv if argv and program(argv[0]) == "loopctl" else None


def finish_stopped(text: str) -> bool:
    argv = strict_loopctl(text)
    return bool(argv) and argv[1:2] == ["finish"] and "--stopped" in argv


def identify_args(text: str) -> tuple[str, str] | None:
    """(kind, unit) of `loopctl guard identify <kind> <unit> [--token T]`, the first command of every brief."""
    argv = strict_loopctl(text)
    if argv and argv[1:3] == ["guard", "identify"] and (len(argv) == 5 or (len(argv) == 7 and argv[5] == "--token")):
        return argv[3], argv[4]
    return None


# --- the walk over one Bash command --------------------------------------------------------------------------------


@dataclass
class Walk:
    """Checks one Bash command; records whether it takes an irreversible step (for the STOP gate)."""

    policy: Policy
    git: Git
    irreversible: bool = False
    seen: list[str] = field(default_factory=list)  # what irreversible steps were seen (messages, tests)

    # -- helpers --
    def _is_main(self, ref: str) -> bool:
        return branch_of(ref) == self.policy.main

    def _inside(self, path: str, cwd: str | None) -> bool:
        full = os.path.normpath(in_dir(cwd, path)) if (cwd or path.startswith("/")) else None
        if full is None:
            return False
        return any(full == d or full.startswith(d + os.sep) for d in self.policy.protected())

    def _step(self, what: str) -> None:
        self.irreversible = True
        self.seen.append(what)

    def _git(self, *args: str, cwd: str | None) -> str:
        try:
            return self.git(*args, cwd=cwd)
        except Exception as exc:  # noqa: BLE001
            raise CheckFailed(f"git {' '.join(args[:3])}: {exc}") from exc

    def _repo_write(self, repo: str | None, what: str, cwd: str | None) -> str | None:
        """A GitHub write by a worker aimed at `repo` (None: the repository of the command's directory)."""
        if repo is None:
            if cwd and self._inside_state(cwd):
                return STATE_WRITE
            if not self.policy.target:
                return only_target(self.policy.target, what)
            self._step(what)
            return None
        if same_repo(repo, self.policy.state):
            return STATE_WRITE
        if not same_repo(repo, self.policy.target):
            return only_target(self.policy.target, f"{what} on {repo}")
        self._step(what)
        return None

    def _inside_state(self, cwd: str) -> bool:
        d = os.path.normpath(self.policy.state_dir or "")
        full = os.path.normpath(cwd)
        return bool(self.policy.state_dir) and (full == d or full.startswith(d + os.sep))

    # -- the shell --
    def shell(self, text: str, cwd: str | None, depth: int = 0) -> str | None:
        action = ACTION_WORDS.search(text) is not None
        paths = not self.policy.controller and bool(self.policy.protected())
        if not action and not paths:
            return None
        if depth > 4:
            return UNREADABLE
        text, docs = split_heredocs(text)
        for doc in docs:
            inner_texts = [doc.body] if doc.to_shell else [] if doc.quoted else substitutions(doc.body)
            for inner in inner_texts:
                reason = self.shell(inner, cwd, depth + 1)
                if reason:
                    return reason
        for inner in substitutions(text):
            reason = self.shell(inner, cwd, depth + 1)
            if reason:
                return reason
        try:
            parsed = commands(text)
        except Unparsed as exc:
            # Can't split it, so don't guess at the rest: refuse only what still reads as a push or a merge.
            if ROUGH_PUSH.search(text) or ROUGH_MERGE.search(text):
                return f"couldn't parse this command ({exc}) and it looks like a push or a merge: split it up"
            if paths and any(d in text for d in self.policy.protected()):
                return WORKER_READS
            return None
        for argv in parsed:
            while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):  # VAR=value prefixes
                argv = argv[1:]
            if not argv:
                continue
            if program(argv[0]) == "cd" and len(argv) > 1:
                cwd = in_dir(cwd, argv[1])
                if paths and self._inside(cwd, None):
                    return WORKER_READS
                continue
            if paths:
                reason = self.paths(argv, cwd)
                if reason:
                    return reason
            if action:
                reason = self.argv(argv, cwd, depth)
                if reason:
                    return reason
        return None

    def paths(self, argv: list[str], cwd: str | None) -> str | None:
        """Worker: no word of a command (but `loopctl`'s) may name a path in the state repo or the engine's docs."""
        if program(argv[0]) == "loopctl":
            return None
        if cwd and self._inside(cwd, None):
            return WORKER_READS
        for token in argv[1:]:
            for word in (token, token.split("=", 1)[1] if "=" in token else ""):
                if word and ("/" in word or word in (".", "..")) and self._inside(word, cwd):
                    return WORKER_READS
        return None

    def argv(self, argv: list[str], cwd: str | None, depth: int) -> str | None:
        """One simple command. Every word that names a pushing, merging or writing program starts a command that is
        checked, so wrappers (`env`, `timeout 9`, `sudo -u x`, `{`, `then` …) need no list of their own."""
        recognised = False
        for j, token in enumerate(argv):
            prog = program(token)
            if j and prog not in STARTS:
                continue
            rest = argv[j:]
            reason = None
            if prog == "env" and (split := env_split_string(rest)) is not None:
                recognised, reason = True, self.shell(split, cwd, depth + 1)
            elif prog == "eval":
                recognised, reason = True, self.shell(" ".join(rest[1:]), cwd, depth + 1)
            elif prog in SHELLS:
                script = shell_string(rest)
                if script is not None:
                    recognised, reason = True, self.shell(script, cwd, depth + 1)
            elif prog == "gh":
                recognised, reason = True, self.gh(rest, cwd)
            elif prog in HTTP_CLIENTS:
                recognised, reason = True, self.http(rest)
            elif prog == "loopctl":
                recognised = True
                if rest[1:2] == ["merge-request"]:
                    self._step("loopctl merge-request")
            elif prog == "git":
                blind = any(program(t) == "xargs" for t in argv[:j])
                git_cwd = env_dir(argv[:j], cwd) if any(program(t) == "env" for t in argv[:j]) else cwd
                recognised, reason = True, self.git_cmd(rest, git_cwd, blind)
            if reason:
                return reason
        if recognised:
            return None
        # Nothing here names git or gh, yet it reads like a push or a merge (`$G push origin HEAD:main`,
        # `$GH pr merge 7`): fail closed.
        if "push" in argv:
            main = self.policy.main
            if self.policy.controller or any(
                t == main or t.endswith((f":{main}", f"/{main}")) or "*" in t for t in argv
            ):
                return UNREADABLE
        if any(argv[k : k + 2] == ["pr", "merge"] for k in range(len(argv))):
            return MERGE_REFUSED
        if self.policy.controller and "$" in argv[0] and ACTION_WORDS.search(" ".join(argv)):
            return UNREADABLE
        return None

    # -- git --
    def git_cmd(self, argv: list[str], cwd: str | None, blind: bool) -> str | None:
        """argv starts at `git`; blind = its arguments come partly from elsewhere (xargs)."""
        i, git_cwd = 1, cwd
        while i < len(argv) and argv[i].startswith("-"):
            if argv[i] == "-C" and i + 1 < len(argv):
                git_cwd = in_dir(cwd, argv[i + 1])
            i += 2 if argv[i] in GIT_OPTS_WITH_VALUE else 1
        sub = argv[i] if i < len(argv) else ""
        publish = sub in ("push", "send-pack", "http-push") or argv[i : i + 2] == ["subtree", "push"]
        if not publish:
            return None
        if self.policy.controller:
            return CONTROLLER_PUSH
        if blind:
            return "a push whose arguments come from xargs can't be checked by the guard: push by name"
        if sub != "push":
            return f"push with `git push <remote> <branch>`: the guard can't check `git {' '.join(argv[i:i + 2])}`"
        return self.push(argv[i + 1 :], git_cwd)

    def push(self, argv: list[str], cwd: str | None) -> str | None:
        """argv is what follows `git push`; cwd is where git runs (worker)."""
        force = delete = False
        positional: list[str] = []
        skip = False
        for arg in argv:
            if skip:
                skip = False
            elif arg == "--":
                continue
            elif arg.startswith("--"):
                name = arg.split("=", 1)[0]
                if name in ALL_BRANCHES:
                    return f"never push {name}: it can rewrite or delete {self.policy.main}; push one branch by name"
                if name == "--repo":
                    return "push to a remote by name (`git push origin <branch>`), not with --repo"
                force = force or name in FORCE_LONG
                delete = delete or name == "--delete"
                skip = name in PUSH_OPTS_WITH_VALUE and "=" not in arg
            elif arg.startswith("-") and len(arg) > 1:
                force = force or "f" in arg[1:]
                delete = delete or "d" in arg[1:]
                skip = arg[-1] == "o"
            else:
                positional.append(arg)
        main = self.policy.main
        refspecs = positional[1:]
        if any(char in spec for spec in positional for char in "$`*"):
            bad = next(spec for spec in positional if any(char in spec for char in "$`*"))
            return f"push branches by name: the guard can't tell where {bad!r} goes (a variable or a pattern)"
        if refspecs:  # a push naming main is refused whatever the remote, so this needs no git call
            reason = self._main_refspecs(refspecs, force, delete, cwd)
            if reason:
                return reason
        upstream = ""
        if positional:
            remote = positional[0]
        else:
            upstream = self._upstream(cwd)
            remote = upstream.split("/", 1)[0] if upstream else "origin"
        reason = self._push_destination(remote, cwd)
        if reason:
            return reason
        if not refspecs:
            # No refspec: git pushes the current branch, to its upstream under push.default=upstream.
            upstream = upstream or self._upstream(cwd)
            spec = f"HEAD:{main}" if upstream.endswith(f"/{main}") else "HEAD"
            reason = self._main_refspecs([spec], force, delete, cwd)
            if reason:
                return reason
        self._step("git push")
        return None

    def _main_refspecs(self, refspecs: list[str], force: bool, delete: bool, cwd: str | None) -> str | None:
        main = self.policy.main
        for spec in refspecs:
            plus = spec.startswith("+")
            src, colon, dst = spec.removeprefix("+").partition(":")
            if not colon:
                dst = src
            if dst in ("HEAD", "@"):
                dst = self._git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).strip()
            if self._is_main(dst):
                if force or plus or delete or not src:
                    return f"never force-push or delete {main}: " + no_main(main)
                return no_main(main)
        return None

    def _upstream(self, cwd: str | None) -> str:
        try:
            return self.git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=cwd).strip()
        except Exception:  # noqa: BLE001  no upstream set
            return ""

    def _push_destination(self, remote: str, cwd: str | None) -> str | None:
        """The repository a push goes to must be the target."""
        if re.search(r"://|@|^[./]|:", remote):
            url = remote
        else:
            url = self._git("remote", "get-url", "--push", remote, cwd=cwd).strip()
        repo = repo_of_url(url)
        if repo is None:
            return f"the guard can't tell which repository {remote!r} is: push to the target's remote by name"
        if same_repo(repo, self.policy.state):
            return "no push to the state repo outside `loopctl`: " + STATE_WRITE
        if not same_repo(repo, self.policy.target):
            return only_target(self.policy.target, f"a push to {repo}")
        return None

    # -- gh --
    def gh(self, argv: list[str], cwd: str | None) -> str | None:
        """argv starts at `gh`."""
        words: list[str] = []
        repo: str | None = None
        i = 1
        while i < len(argv):
            arg = argv[i]
            if arg in GH_OPTS_WITH_VALUE:
                repo = argv[i + 1] if i + 1 < len(argv) else ""
                i += 2
                continue
            if arg.startswith("--repo="):
                repo = arg.split("=", 1)[1]
            elif arg.startswith("-R") and len(arg) > 2:
                repo = arg[2:]
            elif not arg.startswith("-") and len(words) < 2:
                words.append(arg)
                if words[0] == "api":
                    break
            i += 1
        group = words[0] if words else ""
        sub = words[1] if len(words) > 1 else ""
        if group == "api":
            return self.gh_api(argv[i + 1 :] if words else [], cwd)
        if (group, sub) == ("pr", "merge"):
            return MERGE_REFUSED
        if group in ("secret", "variable") and sub not in (GH_READS[group] or set()) | {""}:
            return SECRETS
        reads = GH_READS.get(group, set())
        if not group or reads is None or sub in reads or argv[1:2] in (["--version"], ["--help"], ["-h"]):
            return None
        what = f"`gh {group} {sub}`".replace(" `", "`") if sub else f"`gh {group}`"
        if self.policy.controller:
            return CONTROLLER_WRITE.format(what=what)
        if repo is not None and "/" not in repo:
            return only_target(self.policy.target, f"{what} with --repo {repo!r}")
        return self._repo_write(repo, what, cwd)

    def gh_api(self, args: list[str], cwd: str | None) -> str | None:
        """`gh api …`: GET unless -X/--method says otherwise or a field is sent (then POST), as gh decides."""
        method, endpoint, fields, from_file = None, None, False, False
        i = 0
        while i < len(args):
            arg = args[i]
            name, eq, inline = arg.partition("=") if arg.startswith("--") else (arg[:2], "", arg[2:])
            if arg.startswith("-") and (name in GH_API_OPTS_WITH_VALUE):
                carried = bool(eq or (inline and not arg.startswith("--")))
                value = inline if carried else (args[i + 1] if i + 1 < len(args) else "")
                i += 1 if carried else 2
                if name in ("-X", "--method"):
                    method = value.upper()
                elif name in GH_API_FIELDS:
                    fields = True
                    from_file = from_file or (name in ("-F", "--field") and "=@" in value)
                elif name == "--input":
                    fields, from_file = True, True
                continue
            if not arg.startswith("-") and endpoint is None:
                endpoint = arg
            i += 1
        endpoint = endpoint or ""
        if endpoint.strip("/") == "graphql":
            if any(GRAPHQL_WRITES.search(arg) for arg in args):
                return MERGE_REFUSED if any(GRAPHQL_MERGE.search(arg) for arg in args) else API_WRITE
            if from_file:
                return "the guard can't read a GraphQL query from a file: pass it with -f query='…'"
            if self.policy.controller and not any(re.match(r"query=\s*(query\b|\{)", a) for a in args):
                return CONTROLLER_WRITE.format(what="a GraphQL call that isn't a plain query")
            if any(GRAPHQL_MUTATION.search(arg) for arg in args):
                if self.policy.controller:
                    return CONTROLLER_WRITE.format(what="a GraphQL mutation")
                self._step("GraphQL mutation")  # the repository isn't visible in the query: the gate still applies
            return None
        if (method or ("POST" if fields else "GET")) in ("GET", "HEAD"):
            return None
        if API_WRITE_PATHS.search("/" + endpoint.lstrip("/")):
            return MERGE_REFUSED if REST_MERGE.search(endpoint) else API_WRITE
        if SECRET_PATHS.search(endpoint):
            return SECRETS
        what = f"`gh api` {method or 'POST'} {endpoint}"
        if self.policy.controller:
            return CONTROLLER_WRITE.format(what=what)
        found = REPO_PATH.search(endpoint)
        if not found:
            return only_target(self.policy.target, f"{what} (not under repos/<owner>/<name>)")
        owner, name = found.groups()
        if (owner, name) == ("{owner}", "{repo}"):
            return self._repo_write(None, what, cwd)
        if "$" in owner + name or "{" in owner + name:
            return f"name the repository in {endpoint!r}: the guard can't tell where it writes"
        return self._repo_write(f"{owner}/{name}", what, cwd)

    # -- curl, wget, http, xh --
    def http(self, argv: list[str]) -> str | None:
        """A merge or branch write is refused unless it's a plain GET; other writes to GitHub follow the role."""
        args = argv[1:]
        if any(GRAPHQL_MERGE.search(arg) for arg in args):
            return MERGE_REFUSED
        if any(GRAPHQL_WRITES.search(arg) for arg in args):
            return API_WRITE
        branch_write = any(API_WRITE_PATHS.search(arg) for arg in args)
        github = any(GITHUB_HOST.search(arg) for arg in args)
        if not branch_write and not github:
            return None
        prog = program(argv[0])
        if prog == "curl":
            explicit = next(
                (args[k + 1].upper() for k, a in enumerate(args[:-1]) if a in ("-X", "--request")),
                next((a[2:].upper() for a in args if a.startswith("-X") and len(a) > 2), None),
            )
            write = explicit not in (None, "GET", "HEAD") or any(a.startswith(CURL_WRITE_OPTS) for a in args)
        elif prog == "wget":
            write = any(a.startswith(WGET_WRITE_OPTS) for a in args) or any(
                a.startswith("--method") and not a.upper().endswith(("=GET", "=HEAD")) for a in args
            )
        else:
            write = True  # http/xh: data items and method words are easy to miss; refuse
        if not write:
            return None
        if branch_write:
            return MERGE_REFUSED if any(REST_MERGE.search(arg) for arg in args) else API_WRITE
        if any(SECRET_PATHS.search(arg) for arg in args):
            return SECRETS
        what = f"a {prog} write to GitHub"
        if self.policy.controller:
            return CONTROLLER_WRITE.format(what=what)
        url = next((a for a in args if GITHUB_HOST.search(a)), "")
        found = REPO_PATH.search(url)
        if not found:
            return only_target(self.policy.target, f"{what} (not under /repos/<owner>/<name>)")
        return self._repo_write("/".join(found.groups()), what, None)


# --- tools other than Bash ----------------------------------------------------------------------------------------

MERGE_TOOLS = ("merge_pull_request", "enable_pr_auto_merge")
FILE_TOOLS = ("push_files", "create_or_update_file", "delete_file")
SESSION_TOOLS = ("create_session", "create_trigger", "update_trigger", "delete_trigger", "fire_trigger",
                 "send_later", "interrupt_session", "archive_session")  # fmt: skip
ARTIFACT_DB_WRITES = ("db_write", "db_str_replace")
BUILTIN_REFUSED_IN_WORKERS = ("ArtifactData", "RemoteTrigger")
PATH_TOOLS = {"Read", "Write", "Edit", "MultiEdit", "NotebookRead", "NotebookEdit", "Glob", "Grep", "LS"}
PATH_KEYS = ("file_path", "path", "notebook_path")
PROVIDER_SERVER = re.compile(r"github|gitlab|bitbucket|gitea|azure[-_]?devops", re.IGNORECASE)


def mcp_parts(tool: str) -> tuple[str, str]:
    """(server, tool) of `mcp__<server>__<tool>`."""
    server, _, short = tool[len("mcp__") :].rpartition("__")
    return server, short


def is_read_tool(short: str) -> bool:
    return short.startswith(("get_", "list_", "search_", "read_")) or short.endswith("_read")


@dataclass
class Decision:
    reason: str | None = None
    irreversible: bool = False


def decide_tool(tool: str, tool_input: dict[str, Any], cwd: str | None, policy: Policy, git: Git) -> Decision:
    """The policy for one tool call, without the STOP gate."""
    worker = not policy.controller
    if tool == "Bash":
        walk = Walk(policy, git)
        reason = walk.shell(str(tool_input.get("command") or ""), cwd)
        return Decision(reason, walk.irreversible)
    if tool in BUILTIN_REFUSED_IN_WORKERS:
        return Decision(WORKER_SESSION_TOOL.format(tool=f"`{tool}`") if worker else None)
    if tool in PATH_TOOLS:
        if worker and policy.protected():
            walk = Walk(policy, git)
            for key in PATH_KEYS:
                value = tool_input.get(key)
                if isinstance(value, str) and value and walk._inside(value, cwd):
                    return Decision(WORKER_READS)
        return Decision()
    if not tool.startswith("mcp__"):
        return Decision()
    server, short = mcp_parts(tool)
    if short in SESSION_TOOLS or short == "ArtifactData":
        return Decision(WORKER_SESSION_TOOL.format(tool=f"`{short}`") if worker else None)
    if short in ARTIFACT_DB_WRITES and "artifact" in server.lower():
        return Decision(WORKER_SESSION_TOOL.format(tool=f"`{short}`") if worker else None)
    if short in MERGE_TOOLS:
        return Decision(MERGE_REFUSED)
    if not PROVIDER_SERVER.search(server) or is_read_tool(short):
        return Decision()
    if policy.controller:
        return Decision(CONTROLLER_WRITE.format(what=f"the GitHub write tool `{short}`"))
    owner, name = tool_input.get("owner"), tool_input.get("repo")
    if not (isinstance(owner, str) and isinstance(name, str) and owner and name):
        return Decision(only_target(policy.target, f"`{short}` without an owner and repo"))
    repo = f"{owner}/{name}"
    if same_repo(repo, policy.state):
        return Decision(STATE_WRITE)
    if not same_repo(repo, policy.target):
        return Decision(only_target(policy.target, f"`{short}` on {repo}"))
    if short in FILE_TOOLS and branch_of(str(tool_input.get("branch") or policy.main)) == policy.main:
        return Decision(f"never write to {policy.main} through the GitHub file tools: commit on a branch, push it, "
                        "and let the verify workflow merge")  # fmt: skip
    return Decision(None, True)


def decide(event: dict[str, Any], policy: Policy, git: Git, gate: Gate | None = None) -> str | None:
    """None lets the call through; a string is why it's refused. `gate` is the STOP gate (workers)."""
    try:
        tool = str(event.get("tool_name") or "")
        tool_input = event.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        cwd = event.get("cwd") or None
        command = str(tool_input.get("command") or "") if tool == "Bash" else ""
        if policy.role not in ROLES:
            return f"the guard's role must be worker or controller, not {policy.role!r}"
        if gate is not None and not policy.controller and (who := identify_args(command)):
            refused = gate.identify(*who)
            if refused:
                return refused
        decision = decide_tool(tool, tool_input, cwd, policy, git)
        if decision.reason:
            return decision.reason
        if not policy.controller:
            if gate is None:
                return "the guard has no STOP reader in a worker, so it refuses everything (fail closed)"
            return gate.check(decision.irreversible, finish_stopped(command))
        return None
    except Exception as exc:  # noqa: BLE001  anything unexpected: fail closed
        return f"the guard couldn't check this call, so it's refused: {type(exc).__name__}: {exc}"


# --- the hook entry point -----------------------------------------------------------------------------------------


def policy_from(args: Any, env: dict[str, str]) -> Policy:
    def pick(name: str, var: str, default: str | None = None) -> str | None:
        value = getattr(args, name, None)
        return value if value else env.get(var) or default

    own = str(Path(__file__).resolve().parents[1])
    state = pick("state", "AGENT_LOOP_STATE", "") or ""
    state_dir, engine = pick("state_dir", "AGENT_LOOP_STATE_DIR"), pick("engine_dir", "AGENT_LOOP_ENGINE_DIR", own)
    if state_dir == "auto" or engine == "auto":
        # `auto` (the environments' setup scripts): the attached checkout whose origin is that repository
        from loopctl.install import find_checkout

        if state_dir == "auto":
            found = find_checkout(state, env)
            state_dir = str(found) if found else None
        if engine == "auto":
            found = find_checkout(pick("engine_repo", "AGENT_LOOP_ENGINE_REPO", "") or "", env)
            engine = str(found) if found else own
    return Policy(
        role=pick("role", "AGENT_LOOP_ROLE", "worker") or "worker",
        target=pick("target", "AGENT_LOOP_TARGET", "") or "",
        state=state,
        main=pick("main", "AGENT_LOOP_MAIN", "main") or "main",
        state_dir=state_dir,
        engine_dir=engine,
    )


def add_arguments(parser: Any) -> None:
    parser.add_argument("action", nargs="?", default="hook", choices=["hook", "identify"],
                        help="hook (default): read a PreToolUse event on stdin; identify: see the brief")  # fmt: skip
    parser.add_argument("identity", nargs="*", help="identify: <kind> <unit>")
    parser.add_argument("--token", help="identify: the lease token from your brief; claims the lease for this session")
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--target", help="the target repository, owner/name")
    parser.add_argument("--state", help="the state repository, owner/name")
    parser.add_argument("--main", help="the target's main branch (default main)")
    parser.add_argument("--state-dir", dest="state_dir", help="the state repository's checkout (auto: find it)")
    parser.add_argument("--engine-repo", dest="engine_repo", help="the engine repository, owner/name (for auto)")
    parser.add_argument("--engine-dir", dest="engine_dir",
                        help="agent-loop's checkout (default: this engine's; auto: the attached checkout)")
    parser.add_argument("--cache-dir", dest="cache_dir", help="where the guard keeps its session files")


def run(args: Any, stdin: Any = None, env: dict[str, str] | None = None) -> int:
    from loopctl import guard_io

    env = dict(os.environ) if env is None else env
    if args.action == "identify":
        # The hook records the identity when it sees this exact command; the command itself only confirms.
        # With --token it also claims the unit's lease for this session (decision record 0029, loopctl/claim.py).
        if len(args.identity) != 2:
            print("usage: loopctl guard identify <kind> <unit> [--token T]", file=sys.stderr)
            return 2
        if args.token:
            from loopctl import worker

            state_dir = args.state_dir if args.state_dir and args.state_dir != "auto" else None
            code = worker.claim_main(args.identity[0], args.identity[1], args.token, state=state_dir)
            if code != 0:
                return code
        print(f"identified as {args.identity[0]} {args.identity[1]}")
        return 0
    try:
        event = json.loads((stdin or sys.stdin).read() or "{}")
    except json.JSONDecodeError:
        return 0  # not an event from the platform: nothing to decide
    if not isinstance(event, dict):
        return 0
    policy = policy_from(args, env)
    gate = None
    if not policy.controller:
        cache = Path(args.cache_dir or env.get("AGENT_LOOP_GUARD_CACHE") or guard_io.default_cache_dir())
        url = guard_io.state_url(policy.state, policy.state_dir) if policy.state else ""

        def reader(timeout: float) -> str:
            if not url:
                raise guard_io.GitFailed("no state repository is configured, so STOP can't be read")
            return guard_io.stop_reader(url, cache)(timeout)

        gate = Gate(reader=reader, store=guard_io.FileStore(cache), now_ns=clock.now_ns,
                    session_id=str(event.get("session_id") or "unknown"),
                    env_kind=env.get("AGENT_LOOP_KIND") or None, env_unit=env.get("AGENT_LOOP_UNIT") or None,
                    claim_refused=guard_io.claim_refused_reader(env))  # fmt: skip
    reason = decide(event, policy, guard_io.run_git, gate)
    if reason:
        print(f"agent-loop guard ({policy.role}): {reason}", file=sys.stderr)
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="loopctl guard", description="the agent-loop PreToolUse hook")
    add_arguments(parser)
    return run(parser.parse_args(argv))
