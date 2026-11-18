"""`loopctl tick`, `loopctl ack` and `loopctl status`: the controller's commands (phase2-design §4.1, §12.1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from loopctl import status as status_mod
from loopctl import stop, tick
from loopctl.config import ConfigError, resolve
from loopctl.github import GitHubReader
from loopctl.risk import _get
from loopctl.store import Store, StoreError


def _project(path: str) -> str | None:
    file = Path(path)
    return file.read_text(encoding="utf-8") if file.exists() else None


def _store(path: str, cfg: Any) -> Store:
    """The state clone at `path`, accepted only if its origin is the configured state repo (T-INV-11 a)."""
    url, effective = Store.configured_origin(Path(path))
    want = _get(cfg, "project.state").lower()
    if not want or tick.repo_name(url) != want:
        raise StoreError(f"{path} is a clone of {url or 'nothing'}, not the configured state repo {want or '(unset)'}")
    return Store(Path(path), effective)


def main(args: Any) -> int:
    from loopctl.install import fill_defaults

    fill_defaults(args)
    if args.command == "ack":
        try:
            raw = Path(args.file).read_text(encoding="utf-8") if args.file else args.json_text
            print(f"recorded {tick.record_ack(args.action, json.loads(raw))}")
        except (OSError, ValueError) as exc:
            print(f"loopctl ack: {exc}", file=sys.stderr)
            return 2
        return 0
    text = _project(args.project)
    try:
        cfg = resolve(text, project_file=args.project)
    except ConfigError as exc:
        if args.command == "status":
            print(f"loopctl status: {exc}", file=sys.stderr)
            return 1
        cfg = resolve()
    try:
        store = _store(args.state, cfg)
        reader = None if args.offline else GitHubReader(_get(cfg, "project.repo"))
        if args.command == "tick":
            tick.run(store, project_text=text, project_file=args.project, reader=reader)
            return 0
        if args.command == "ingest-commands":
            from loopctl import commands

            docs = commands.documents(json.loads(Path(args.file).read_text(encoding="utf-8")))
            state_reader = None if args.offline else GitHubReader(_get(cfg, "project.state"))
            commands.run(store, docs=docs, project_text=text, project_file=args.project, reader=state_reader,
                         scratch=tick.scratch_dir(), out_file=sys.stdout)  # fmt: skip
            return 0
        store.sync()
        state = store.read_state()
        from loopctl import commands

        cfg = commands.effective(text, state, args.project)
        snap = None
        if reader is not None:
            from loopctl import snapshot

            snap = snapshot.read(reader, state, _get(cfg, "project.main"))
        install = json.loads(store.read_file(tick.INSTALL_FILE) or "null")
        doc = status_mod.build(state, cfg, snapshot=snap, stop_text=store.read_file(stop.FILE), install=install,
                               tick_id=None, events=store.read_events(status_mod.recent_days()))  # fmt: skip
    except (StoreError, OSError, ValueError) as exc:
        print(f"loopctl {args.command}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(doc, indent=1, ensure_ascii=False))
    elif args.unit:
        sys.stdout.write(status_mod.unit_text(doc, args.unit))
    else:
        sys.stdout.write(status_mod.text(doc))
    return 0
