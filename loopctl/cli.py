"""The `loopctl` command line. Subcommands arrive milestone by milestone (docs/v2/build-plan.md §3)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from loopctl import __version__

# Planned subcommands and the milestone that builds each; calling one early says so instead of failing obscurely.
PLANNED: dict[str, str] = {}
WORKER = ("event", "finish", "review", "gate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loopctl", description="agent-loop v2 control plane")
    parser.add_argument("--version", action="version", version=f"loopctl {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.add_parser("version", help="print the version")
    explain = sub.add_parser("explain", help="each effective setting, its source and what it overrides")
    explain.add_argument("key", nargs="?", help="one dotted key, e.g. budget.week (default: every key)")
    explain.add_argument("--project", default=".agent-loop.toml", help="the target's project file (default: %(default)s)")
    from loopctl import brief

    brief.add_arguments(sub.add_parser("brief", help="the whole prompt of a worker session, under its token budget"))
    from loopctl import guard

    guard.add_arguments(sub.add_parser("guard", help="the PreToolUse hook of workers and the controller"))
    tick = sub.add_parser("tick", help="one controller tick: decide, write the state repo, print the actions")
    _where(tick)
    ack = sub.add_parser("ack", help="report the result of an action the last tick printed")
    ack.add_argument("action", help="the action's id as printed")
    src = ack.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="a JSON file with the tool's result")
    src.add_argument("--json", dest="json_text", help="the tool's result as JSON text")
    status = sub.add_parser("status", help="what runs, what waits for you, why slots idle, spend, queue (§12.1)")
    _where(status)
    status.add_argument("--json", action="store_true", help="the status document the page renders")
    status.add_argument("--unit", help="one unit's state and history")
    ingest = sub.add_parser("ingest-commands", help="apply the page's new commands in send order; print write-backs")
    _where(ingest)
    ingest.add_argument("file", help="the `ArtifactData list commands` result saved as JSON")
    from loopctl import worker

    worker.add_arguments(sub)
    from loopctl import verify

    verify.add_arguments(sub.add_parser("verify", help="the verify workflow's decisions (agent-loop.yml, §10.2)"))
    from loopctl import reconcile

    reconcile.add_arguments(sub)
    from loopctl import install

    install.add_arguments(sub.add_parser("install", help="setup scripts, the standing instruction, install.json"))
    for name, milestone in PLANNED.items():
        sub.add_parser(name, help=f"not built yet ({milestone})", add_help=False)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args, _rest = parser.parse_known_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    if args.command == "version":
        print(f"loopctl {__version__}")
        return 0
    if args.command == "explain":
        return _explain(args.key, args.project)
    if args.command == "brief":
        from loopctl import brief

        return brief.run(parser.parse_args(argv))
    if args.command == "guard":
        from loopctl import guard

        return guard.run(parser.parse_args(argv))
    if args.command in WORKER:
        from loopctl import worker

        return worker.main(parser.parse_args(argv))
    if args.command == "verify":
        from loopctl import verify

        return verify.run(parser.parse_args(argv))
    if args.command == "reconcile":
        from loopctl import reconcile

        return reconcile.run(parser.parse_args(argv))
    if args.command == "install":
        from loopctl import install

        return install.run(parser.parse_args(argv))
    if args.command in ("tick", "ack", "status", "ingest-commands"):
        from loopctl import controller_cli

        return controller_cli.main(parser.parse_args(argv))
    print(f"loopctl {args.command}: not built yet (milestone {PLANNED[args.command]})", file=sys.stderr)
    return 2


def _where(p: argparse.ArgumentParser) -> None:
    p.add_argument("--state", default=None,
                   help="the state repo's clone (default: $LOOPCTL_STATE_DIR, else the attached checkout named in "
                        "~/.agent-loop/role.json, else .)")  # fmt: skip
    p.add_argument("--project", default=None,
                   help="the target's .agent-loop.toml (default: $LOOPCTL_PROJECT, else the attached target "
                        "checkout's, else ./.agent-loop.toml)")  # fmt: skip
    p.add_argument("--offline", action="store_true", help="don't read GitHub (tests, or when GitHub is down)")


def _explain(key: str | None, project: str) -> int:
    from pathlib import Path

    from loopctl.config import ConfigError, resolve

    path = Path(project)
    try:
        cfg = resolve(path.read_text(encoding="utf-8") if path.exists() else None, project_file=str(path))
        print(cfg.explain(key))
        if key is None:
            print(f"config_hash = {cfg.hash}")
    except ConfigError as exc:
        print(f"loopctl explain: {exc}", file=sys.stderr)
        return 1
    return 0
