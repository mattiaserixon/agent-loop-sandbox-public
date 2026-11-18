"""`loopctl install`: the runbook install path (M0.13; phase2-design §15, build-plan G5, docs/v2/controller-runbook.md).

The one-command install (§15) is cut until after M5; this is what the runbook uses in its place:

- `loopctl install setup-script --role worker|controller …` prints the exact setup script for an environment
  (`templates/setup/environment.sh`): it installs `loopctl` from the engine repository at a pinned commit and
  `loopctl guard --role <role>` as a PreToolUse hook for every tool (no matcher) in the container's user settings.
  The hook fails closed (engine missing, at another commit, or crashing → every tool call is refused).
- `loopctl install instruction` prints the controller's standing instruction (one sentence; everything else the
  controller does is printed by `loopctl tick`).
- `loopctl install write …` writes the state repo's `install.json` (schema `install`): `engine_repo`, `engine_ref`,
  `controller_environment`, `worker_environment` (the tick's self-check and its `create_session` actions),
  `control_issue`, `app_login` (whose verified commits are trusted as confirmations; null until the App exists),
  `page` (null until the page is published) and `workflow_engine` (where `agent-loop.yml` gets the engine). One CAS
  commit to the state repo, through the store.
- `loopctl install vendor-manifest` / `check-vendor`: the sandbox's engine source (coordinator decision, M0.13). The
  sandbox App is installed on the two sandbox repositories only, so the sandbox workflow can't fetch the engine from
  agent-loop: a copy of `loopctl/` at a pinned engine commit is committed into the sandbox target
  (`scripts/vendor-engine.sh`, which runs `git archive` and then `vendor-manifest`), and the workflow is rendered with
  `ENGINE_REPO` = the target and `ENGINE_SHA` = the target commit holding the copy. `check-vendor` proves the copy is
  unchanged. The target's `.agent-loop.toml` must list `loopctl/**` in `risk.gate_files` and `risk.critical`, so a
  PR that edits the copy needs the owner's app-free approval (precondition 6); `vendor-manifest` refuses otherwise.

It also finds the attached checkouts in a session (`find_checkout`, `default_path`): the setup script records the
role and the repositories in `~/.agent-loop/role.json`; `loopctl`'s `--state`, `--project` and `--target-dir` and the
guard's `--state-dir auto` / `--engine-dir auto` default to the checkouts whose `origin` is that repository.

Standard library only, no network or processes (the setup script and `scripts/vendor-engine.sh` run git).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from loopctl import clock, schema

ROOT = Path(__file__).resolve().parent
SETUP_TEMPLATE = ROOT / "templates" / "setup" / "environment.sh"
INSTRUCTION_FILE = ROOT / "templates" / "setup" / "controller-instruction.txt"
INSTALL_FILE = "install.json"
VENDOR_FILE = "VENDOR.json"
VENDOR_DIR = "loopctl"
ROLES = ("worker", "controller")
REPO = re.compile(r"^[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
PLACEHOLDER = re.compile(r"__[A-Z_]+__")
ROOTS = ("/home/user", "/workspace", "/workspaces", "/repos")


class InstallError(ValueError):
    """An input or a file the install can't accept; nothing was written."""


# ------------------------------------------------------------------------------------------- the setup scripts
def render_setup(*, role: str, engine_repo: str, engine_ref: str, target: str, state: str, main: str = "main",
                 template: str | None = None) -> str:  # fmt: skip
    """The environment's setup script for `role`, with every value checked before it goes into shell text."""
    if role not in ROLES:
        raise InstallError(f"role {role!r} is not one of {', '.join(ROLES)}")
    for name, repo in (("engine", engine_repo), ("target", target), ("state", state)):
        if not REPO.match(repo):
            raise InstallError(f"the {name} repository {repo!r} is not owner/name")
    if not SHA.match(engine_ref):
        raise InstallError("the engine is pinned by a full commit sha (40 hex characters)")
    if not BRANCH.match(main) or main.startswith("-"):
        raise InstallError(f"{main!r} is not a branch name")
    values = {"__ROLE__": role, "__ENGINE_REPO__": engine_repo, "__ENGINE_REF__": engine_ref,
              "__TARGET_REPO__": target, "__STATE_REPO__": state, "__MAIN__": main}  # fmt: skip
    text = SETUP_TEMPLATE.read_text(encoding="utf-8") if template is None else template
    for key, value in values.items():
        text = text.replace(key, value)
    left = PLACEHOLDER.findall(text)
    if left:
        raise InstallError(f"the setup template has unfilled placeholders: {sorted(set(left))}")
    return text


def instruction() -> str:
    """The controller's standing instruction, pasted by the owner as the session's first message (runbook §3)."""
    return INSTRUCTION_FILE.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------------------------- install.json
def install_doc(*, engine_repo: str, engine_ref: str, controller_environment: str, worker_environment: str,
                control_issue: int, app_login: str | None = None, page: str | None = None,
                workflow_source: str = "engine_repo", target: str | None = None,
                vendored_ref: str | None = None) -> dict[str, Any]:  # fmt: skip
    """install.json, validated. `workflow_source="vendored"` needs the target (where the copy lives) and the engine
    commit the copy was taken from (default: `engine_ref`)."""
    if workflow_source == "vendored":
        if not target:
            raise InstallError("a vendored workflow engine lives in the target: give --target")
        workflow = {"source": "vendored", "repo": target, "path": VENDOR_DIR, "engine_ref": vendored_ref or engine_ref}
    elif workflow_source == "engine_repo":
        workflow = {"source": "engine_repo", "repo": engine_repo, "engine_ref": engine_ref}
    else:
        raise InstallError(f"workflow engine source {workflow_source!r} is not engine_repo or vendored")
    if controller_environment == worker_environment:
        raise InstallError("the controller and the workers need separate environments (phase2-design §4.4)")
    doc = {"version": 1, "engine_repo": engine_repo, "engine_ref": engine_ref,
           "controller_environment": controller_environment, "worker_environment": worker_environment,
           "control_issue": control_issue, "app_login": app_login or None, "page": page or None,
           "workflow_engine": workflow, "installed_at": clock.stamp()}  # fmt: skip
    try:
        schema.validate(doc, "install")
    except schema.SchemaError as exc:
        raise InstallError(f"install.json would be invalid: {exc}") from exc
    return doc


def differences(old: Mapping[str, Any] | None, new: Mapping[str, Any]) -> list[str]:
    """What a rewrite changes (installed_at aside), as `key: old -> new` lines."""
    old = old or {}
    keys = sorted((set(old) | set(new)) - {"installed_at"})
    return [f"{k}: {json.dumps(old.get(k))} -> {json.dumps(new.get(k))}" for k in keys if old.get(k) != new.get(k)]


def write_install(store: Any, doc: Mapping[str, Any], *, force: bool = False) -> list[str]:
    """One CAS commit of install.json to the state repo. An existing file with other values is replaced only with
    `force` (a restart in other environments, the App's login once it exists). Returns the differences."""
    from loopctl.store import Write

    schema.validate(dict(doc), "install")
    seen: dict[str, list[str]] = {}

    def decide(_state: dict[str, Any], st: Any) -> Write | None:
        raw = st.read_file(INSTALL_FILE)
        try:
            old = json.loads(raw) if raw else None
        except ValueError:
            old = {"unreadable": raw[:40]}
        diff = differences(old, doc)
        seen["diff"] = diff
        if old is not None and not diff:
            return None
        if old is not None and not force:
            raise InstallError(f"{INSTALL_FILE} exists with other values (pass --force to replace them): "
                               + "; ".join(diff))  # fmt: skip
        return Write(files={INSTALL_FILE: json.dumps(dict(doc), indent=1, sort_keys=True) + "\n"})

    store.transact(decide, "loopctl install: install.json")
    return seen.get("diff", [])


# ------------------------------------------------------------------------------------ attached checkouts in a session
def role_file(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("AGENT_LOOP_ROLE_FILE"):
        return Path(env["AGENT_LOOP_ROLE_FILE"])
    home = env.get("AGENT_LOOP_HOME") or str(Path(env.get("HOME") or Path.home()) / ".agent-loop")
    return Path(home) / "role.json"


def role_config(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """`~/.agent-loop/role.json`, written by the environment's setup script ({} when absent or unreadable)."""
    try:
        doc = json.loads(role_file(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _origin(checkout: Path) -> str | None:
    """remote.origin.url from a checkout's .git/config, read as a file (the guard calls this on every tool call)."""
    try:
        text = (checkout / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.strip("[]").replace('"', "").split()
        elif section == ["remote", "origin"] and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() == "url":
                return value.strip()
    return None


def _name(url: str) -> str | None:
    m = re.search(r"(?:github\.com[/:])?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", url.strip())
    return m.group(1).lower() if m else None


def search_roots(env: Mapping[str, str] | None = None, cwd: str | None = None) -> list[Path]:
    env = os.environ if env is None else env
    here = Path(cwd or os.getcwd())
    roots = [Path(r) for r in (env.get("AGENT_LOOP_REPOS_ROOT") or "").split() if r]
    roots += [here, here.parent, Path(env.get("HOME") or Path.home())] + [Path(r) for r in ROOTS]
    return list(dict.fromkeys(roots))


def find_checkout(repo: str, env: Mapping[str, str] | None = None, cwd: str | None = None) -> Path | None:
    """The checkout of `repo` (owner/name) among the search roots and their children, by its origin's name."""
    want = (repo or "").lower()
    if not REPO.match(want):
        return None
    for root in search_roots(env, cwd):
        try:
            candidates = [root] + sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            continue
        for co in candidates:
            if (co / ".git").exists():
                url = _origin(co)
                if url and _name(url) == want:
                    return co
    return None


M2_PROJECT = "project.toml"  # the project config in the state repo while the target holds none (M2)


def default_path(kind: str, env: Mapping[str, str] | None = None, cwd: str | None = None) -> str:
    """Default of `--state` (the state clone), `--project` (the target's .agent-loop.toml, else the state repo's
    project.toml when the target has none: M2) and `--target-dir`: the environment variable, else the attached
    checkout named by role.json, else the current directory's."""
    env = os.environ if env is None else env
    var = {"state": "LOOPCTL_STATE_DIR", "project": "LOOPCTL_PROJECT", "target": "LOOPCTL_TARGET_DIR"}[kind]
    if env.get(var):
        return env[var]
    role = role_config(env)
    repo = role.get("state") if kind == "state" else role.get("target")
    found = find_checkout(repo, env, cwd) if isinstance(repo, str) else None
    if kind == "project":
        if found and (found / ".agent-loop.toml").exists():
            return str(found / ".agent-loop.toml")
        # Through M2 nothing is written to the target: the project config is the state repo's project.toml
        # (build-plan §4 M2), used when the target has no .agent-loop.toml yet.
        state = role.get("state")
        home = find_checkout(state, env, cwd) if isinstance(state, str) else None
        if home and (home / M2_PROJECT).exists():
            return str(home / M2_PROJECT)
        return str(found / ".agent-loop.toml") if found else ".agent-loop.toml"
    return str(found) if found else "."


def fill_defaults(args: Any, env: Mapping[str, str] | None = None) -> Any:
    """Fill the `state`, `project` and `target_dir` arguments left unset on the command line."""
    for attr, kind in (("state", "state"), ("project", "project"), ("target_dir", "target")):
        if hasattr(args, attr) and getattr(args, attr) is None:
            setattr(args, attr, default_path(kind, env))
    return args


# --------------------------------------------------------------------------------------- the vendored engine
def _files(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise InstallError(f"{rel} is a symlink: a vendored engine holds regular files only")
        if not path.is_file() or rel == VENDOR_FILE or "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def gates_the_copy(project_text: str | None) -> list[str]:
    """What the target's .agent-loop.toml lacks for a vendored engine ([] = nothing)."""
    from loopctl.config import resolve
    from loopctl.risk import any_match

    cfg = resolve(project_text)
    probe = f"{VENDOR_DIR}/verify.py"
    missing = []
    if not any_match(probe, list(cfg.value("risk.gate_files"))):
        missing.append(f'risk.gate_files must include "{VENDOR_DIR}/**"')
    if not any_match(probe, list(cfg.value("risk.critical"))):
        missing.append(f'risk.critical must include "{VENDOR_DIR}/**"')
    return missing


def vendor_manifest(copy: Path, *, engine_repo: str, engine_ref: str, project_text: str | None) -> dict[str, Any]:
    """Write VENDOR.json into the vendored copy (`<target>/loopctl`): the engine commit and every file's sha256."""
    if not REPO.match(engine_repo) or not SHA.match(engine_ref):
        raise InstallError("give the engine repository as owner/name and its commit as a full sha")
    missing = gates_the_copy(project_text)
    if missing:
        raise InstallError("the target's .agent-loop.toml doesn't protect the vendored engine: " + "; ".join(missing))
    copy = Path(copy)
    if copy.name != VENDOR_DIR or not (copy / "bin" / "loopctl").is_file():
        raise InstallError(f"{copy} is not a vendored engine (expected <target>/{VENDOR_DIR} with bin/loopctl)")
    doc = {"engine_repo": engine_repo, "engine_ref": engine_ref, "files": _files(copy)}
    (copy / VENDOR_FILE).write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return doc


def check_vendor(copy: Path, *, engine_ref: str | None = None, against: Path | None = None) -> list[str]:
    """Problems with a vendored copy ([] = intact): files added, removed or changed since VENDOR.json was written,
    a different engine commit than `engine_ref`, or files that differ from `against` (the engine's `loopctl/`
    checked out at that commit)."""
    copy = Path(copy)
    try:
        doc = json.loads((copy / VENDOR_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{VENDOR_FILE} can't be read: {exc}"]
    problems = []
    if engine_ref and doc.get("engine_ref") != engine_ref:
        problems.append(f"the copy is of {doc.get('engine_ref')}, not {engine_ref}")
    want, have = doc.get("files") or {}, _files(copy)
    problems += [f"changed: {p}" for p in sorted(set(want) & set(have)) if want[p] != have[p]]
    problems += [f"added: {p}" for p in sorted(set(have) - set(want))]
    problems += [f"removed: {p}" for p in sorted(set(want) - set(have))]
    if against is not None:
        ref = _files(Path(against))
        problems += [f"differs from the engine: {p}" for p in sorted(set(ref) | set(have)) if ref.get(p) != have.get(p)]
    return problems


# ---------------------------------------------------------------------------------------------------- the CLI
def add_arguments(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="install_command", metavar="<step>", required=True)
    s = sub.add_parser("setup-script", help="print an environment's setup script (paste it into the environment)")
    s.add_argument("--role", required=True, choices=ROLES)
    s.add_argument("--engine-repo", required=True, help="owner/agent-loop")
    s.add_argument("--engine-ref", required=True, help="the engine commit to pin (full sha)")
    s.add_argument("--target", required=True, help="the target repository, owner/name")
    s.add_argument("--state", required=True, help="the state repository, owner/name")
    s.add_argument("--main", default="main", help="the target's main branch (default %(default)s)")
    sub.add_parser("instruction", help="print the controller's standing instruction")
    w = sub.add_parser("write", help="write install.json to the state repo (one commit)")
    w.add_argument("--state-dir", required=True, help="the state repo's clone")
    w.add_argument("--project", help="the target's .agent-loop.toml (its project.state must be the clone's origin)")
    w.add_argument("--engine-repo", required=True)
    w.add_argument("--engine-ref", required=True)
    w.add_argument("--controller-environment", required=True, help="env_… of the controller environment")
    w.add_argument("--worker-environment", required=True, help="env_… of the worker environment")
    w.add_argument("--control-issue", required=True, type=int, help="the target's control issue number")
    w.add_argument("--app-login", help="<app-slug>[bot] (leave out until the App exists)")
    w.add_argument("--page", help="the page's artifact slug (leave out until it is published)")
    w.add_argument("--workflow-engine", choices=("engine_repo", "vendored"), default="engine_repo")
    w.add_argument("--target", help="the target, owner/name (needed with --workflow-engine vendored)")
    w.add_argument("--vendored-ref", help="the engine commit the vendored copy was taken from (default --engine-ref)")
    w.add_argument("--force", action="store_true", help="replace an install.json with other values")
    w.add_argument("--dry-run", action="store_true", help="print install.json, write nothing")
    v = sub.add_parser("vendor-manifest", help="write VENDOR.json into a vendored copy (scripts/vendor-engine.sh)")
    v.add_argument("copy", help="<target clone>/loopctl")
    v.add_argument("--engine-repo", required=True)
    v.add_argument("--engine-ref", required=True)
    v.add_argument("--project", required=True, help="the target's .agent-loop.toml")
    c = sub.add_parser("check-vendor", help="check that a vendored copy is unchanged")
    c.add_argument("copy", help="<target clone>/loopctl")
    c.add_argument("--engine-ref", help="the engine commit it must be a copy of")
    c.add_argument("--against", help="the engine's loopctl/ at that commit, to compare file by file")


def run(args: Any) -> int:
    cmd = args.install_command
    try:
        if cmd == "setup-script":
            sys.stdout.write(render_setup(role=args.role, engine_repo=args.engine_repo, engine_ref=args.engine_ref,
                                          target=args.target, state=args.state, main=args.main))  # fmt: skip
            return 0
        if cmd == "instruction":
            print(instruction())
            return 0
        if cmd == "write":
            return _write(args)
        if cmd == "vendor-manifest":
            project = Path(args.project)
            text = project.read_text(encoding="utf-8") if project.exists() else None
            doc = vendor_manifest(Path(args.copy), engine_repo=args.engine_repo, engine_ref=args.engine_ref,
                                  project_text=text)  # fmt: skip
            print(f"wrote {Path(args.copy) / VENDOR_FILE}: {len(doc['files'])} files of {args.engine_repo}@{args.engine_ref}")
            return 0
        if cmd == "check-vendor":
            problems = check_vendor(Path(args.copy), engine_ref=args.engine_ref,
                                    against=Path(args.against) if args.against else None)  # fmt: skip
            for line in problems:
                print(line)
            print("vendored engine intact" if not problems else f"{len(problems)} problem(s)", file=sys.stderr)
            return 1 if problems else 0
    except (InstallError, OSError) as exc:
        print(f"loopctl install {cmd}: {exc}", file=sys.stderr)
        return 1
    return 2


def _write(args: Any) -> int:
    from loopctl.store import Store, StoreError

    doc = install_doc(engine_repo=args.engine_repo, engine_ref=args.engine_ref,
                      controller_environment=args.controller_environment,
                      worker_environment=args.worker_environment, control_issue=args.control_issue,
                      app_login=args.app_login, page=args.page, workflow_source=args.workflow_engine,
                      target=args.target, vendored_ref=args.vendored_ref)  # fmt: skip
    if args.dry_run:
        print(json.dumps(doc, indent=1, sort_keys=True))
        return 0
    path = Path(args.state_dir)
    url, effective = Store.configured_origin(path)
    if not url:
        raise InstallError(f"{path} is not a clone with an origin")
    if args.project:
        from loopctl.config import resolve

        project = Path(args.project)
        want = resolve(project.read_text(encoding="utf-8") if project.exists() else None).value("project.state")
        if not want or _name(url) != str(want).lower():
            raise InstallError(f"{path} is a clone of {url}, not the configured state repo {want or '(unset)'}")
    try:
        diff = write_install(Store(path, effective), doc, force=args.force)
    except StoreError as exc:
        raise InstallError(str(exc)) from exc
    print(f"install.json written to {_name(url)}" + (": " + "; ".join(diff) if diff else ""))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loopctl install")
    add_arguments(parser)
    return run(parser.parse_args(argv))
