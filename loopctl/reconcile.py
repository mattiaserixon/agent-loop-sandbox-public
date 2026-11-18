"""`loopctl reconcile`: the store compared with GitHub fact by fact, in code (phase2-design §5.2; proof test K-RECON).

The daily reconcile unit used to carry the store's facts in its brief and compare them in a model; with ensemble's
~250 units that brief overflowed (T-REPLAY, 2026-11-18: all four reconcile units became owner questions). Now the
unit runs this command and reports its result (`loopctl event <unit> --token T reconcile-report --file …`), so its
brief has a fixed size whatever the store holds.

`read(reader, …)` makes v2's view of GitHub, a plain JSON document, with `GitHubReader.get` only (T-INV-11 a): every
issue and PR (state, labels, head, merge, times), main's sha, and the changed paths of the open PRs whose tier is
checked. `compare(state, view, …)` is pure: one check per fact of §5.2 that GitHub also holds, per unit. A difference
is a **mismatch** unless GitHub changed after the last tick (`state.json` `last_tick_at`), when the next tick ingests
it: then it is **pending**, not a mismatch. §5.2's facts that only the store or the platform holds are listed in
`not_compared` with the reason, so the report says what it did not check.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from loopctl import clock
from loopctl.risk import _get, assess

PER_PAGE = 100
PAGES = 50  # 5,000 issues and PRs; past that the list is incomplete and nothing is concluded from a missing item
_NEXT = re.compile(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="next"')
OPEN_STATES = ("in_review", "changes", "approved", "gating")  # the unit's head must be the PR's head
UNWORKED = ("new", "triaged", "queued")
CLOSED_VERDICTS = ("rejected", "duplicate")  # triage closed the unit; the issue may stay open on GitHub

# §5.2's fact table, row by row: what this command checks for each, or why it can't (tests/test_reconcile.py ties
# these rows to the design's table).
FACTS: dict[str, dict[str, Any]] = {
    "unit state, lease, attempt": {"checks": ["unit_state", "pr_head", "unknown_to_store"]},
    "liveness of a worker": {"reason": "held by the platform (session status), not by GitHub"},
    "review round and findings raised": {"reason": "held only by the store (events); GitHub has no copy to compare"},
    "a finding's fix claim": {"reason": "held only by the store (events)"},
    "a finding's waiver": {"reason": "held only by the store (events)"},
    "gate result on sha": {"reason": "held only by the store until M3, when the verify workflow posts "
                                     "`agent-loop/verify` statuses on the gated sha"},
    "main green": {"checks": ["main_sha"]},
    "merge": {"checks": ["merge"]},
    "spend per session": {"reason": "held by the platform (session costs), not by GitHub"},
    "effective config": {"checks": ["config"]},
    "owner command": {"reason": "held by the store; confirmations are the state repo's own files"},
    "question to the owner": {"reason": "held only by the store"},
    "low-priority note": {"reason": "held only by the store (notes.json)"},
}
# the risk tier is part of the unit's record the tick computes from GitHub's changed paths
EXTRA_CHECKS = ["pr_risk"]


class ReconcileError(RuntimeError):
    """GitHub couldn't be read: nothing is concluded from a partial picture."""


# ------------------------------------------------------------------------------------------------------- the view
def _pages(reader: Any, path: str, params: Mapping[str, Any]) -> tuple[list[Any], bool]:
    out: list[Any] = []
    page = 1
    for _ in range(PAGES):
        reply = reader.get(path, {**params, "per_page": PER_PAGE, "page": page})
        if reply.status != 200 or not isinstance(reply.data, list):
            raise ReconcileError(f"GET {path} answered {reply.status}")
        out += reply.data
        link = {k.lower(): v for k, v in reply.headers.items()}.get("link", "")
        nxt = _NEXT.search(link)
        if not nxt:
            return out, True
        page = int(nxt.group(1))
    return out, False


def read(reader: Any, state: Mapping[str, Any], main: str = "main") -> dict[str, Any]:
    """v2's view of GitHub for `compare`: GETs only."""
    issues, issues_complete = _pages(reader, "/issues", {"state": "all"})
    prs, prs_complete = _pages(reader, "/pulls", {"state": "all"})
    head = reader.get(f"/commits/{main}")
    if head.status != 200 or not isinstance(head.data, dict):
        raise ReconcileError(f"GET /commits/{main} answered {head.status}")
    view: dict[str, Any] = {
        "main": {"sha": head.data.get("sha"), "at": ((head.data.get("commit") or {}).get("committer") or {}).get("date")},
        "issues": [{"number": i["number"], "state": i.get("state"), "labels": sorted(lb["name"] for lb in i.get("labels") or []),
                    "created_at": i.get("created_at"), "closed_at": i.get("closed_at"), "updated_at": i.get("updated_at")}
                   for i in issues if "pull_request" not in i],  # fmt: skip
        "issues_complete": issues_complete,
        "prs": [{"number": p["number"], "state": p.get("state"), "head": (p.get("head") or {}).get("sha"),
                 "branch": (p.get("head") or {}).get("ref") or "", "draft": bool(p.get("draft")),
                 "merged_at": p.get("merged_at"), "merged_sha": p.get("merge_commit_sha") if p.get("merged_at") else None,
                 "created_at": p.get("created_at"), "closed_at": p.get("closed_at"), "updated_at": p.get("updated_at"),
                 "labels": sorted(lb["name"] for lb in p.get("labels") or []), "files": None} for p in prs],  # fmt: skip
        "prs_complete": prs_complete,
    }
    store_main = (state.get("main") or {}).get("sha")
    if store_main and view["main"]["sha"] and store_main != view["main"]["sha"]:
        cmp = reader.get(f"/compare/{store_main}...{view['main']['sha']}")
        if cmp.status == 200 and isinstance(cmp.data, dict):
            view["main"]["compare"] = {"status": cmp.data.get("status"), "first_new_at": min(
                (((c.get("commit") or {}).get("committer") or {}).get("date") or "" for c in cmp.data.get("commits") or []),
                default=None)}  # fmt: skip
    by_pr = {u["pr"]: u for u in state["units"].values() if u.get("pr") is not None}
    for pr in view["prs"]:
        unit = by_pr.get(pr["number"])
        if pr["state"] == "open" and unit is not None and unit["state"] in OPEN_STATES and unit.get("head") == pr["head"]:
            files, _ = _pages(reader, f"/pulls/{pr['number']}/files", {})
            pr["files"] = [{"path": f["filename"], "patch": f.get("patch")} for f in files]
    return view


# ------------------------------------------------------------------------------------------------ the comparison
def _t(text: Any) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def compare(state: Mapping[str, Any], view: Mapping[str, Any], *, cfg: Any, control_issue: int | None = None,
            config_hash: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The report: `mismatches` ({fact, github, explanation}, the `reconcile.reported` shape), `pending` (GitHub
    changed after the last tick), `checked` (how many facts were compared, by check) and `not_compared`."""
    last = _t(state.get("last_tick_at"))
    mismatches: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    checked: dict[str, int] = {c: 0 for row in FACTS.values() for c in row.get("checks", [])}
    checked.update({c: 0 for c in EXTRA_CHECKS})

    def after(*times: Any) -> bool:
        """GitHub changed this after the last tick: the next tick ingests it."""
        ts = [t for t in (_t(x) for x in times) if t]
        return bool(last and ts and max(ts) > last)

    def differ(check: str, unit: str | None, store: Any, github: Any, why: str, *changed: Any) -> None:
        row = {"fact": {"check": check, "unit": unit, "store": store}, "github": github, "explanation": why[:500]}
        (pending if after(*changed) else mismatches).append(row)

    issues = {i["number"]: i for i in view.get("issues", [])}
    prs = {p["number"]: p for p in view.get("prs", [])}
    units = state["units"]
    owned = {u["pr"] for u in units.values() if u.get("pr") is not None}

    for key, unit in sorted(units.items()):
        st = unit["state"]
        if key.startswith("issue-"):
            n = int(key.split("-")[1])
            issue = issues.get(n)
            if issue is None:
                if view.get("issues_complete"):
                    checked["unit_state"] += 1
                    differ("unit_state", key, st, None, f"issue #{n} isn't on GitHub (deleted or transferred)")
            elif unit.get("pr") is None:
                checked["unit_state"] += 1
                if issue["state"] == "closed" and st not in ("closed", "merged") and not unit.get("lease"):
                    differ("unit_state", key, st, "closed", f"issue #{n} is closed on GitHub; the store has {key} "
                           f"{st}", issue.get("closed_at"))  # fmt: skip
                elif issue["state"] == "open" and st == "closed" and unit.get("verdict") not in CLOSED_VERDICTS:
                    differ("unit_state", key, st, "open", f"issue #{n} is open on GitHub (reopened?) but {key} is "
                           "closed in the store", issue.get("updated_at"))  # fmt: skip
        n = unit.get("pr")
        if n is None:
            continue
        pr = prs.get(n)
        if pr is None:
            if view.get("prs_complete"):
                checked["merge"] += 1
                differ("merge", key, st, None, f"PR #{n} isn't on GitHub")
            continue
        checked["merge"] += 1
        violated = st == "needs_owner" and str(unit.get("violation") or "").startswith("merged around the gate")
        if pr.get("merged_at"):
            if st != "merged" and not violated:
                differ("merge", key, st, "merged", f"PR #{n} merged on GitHub at {pr['merged_at']}; the store has "
                       f"{key} {st}", pr.get("merged_at"))  # fmt: skip
            continue
        if st == "merged":
            differ("merge", key, st, pr["state"], f"the store has {key} merged; PR #{n} is {pr['state']} and unmerged "
                   "on GitHub")  # fmt: skip
            continue
        checked["unit_state"] += 1
        if pr["state"] == "closed":
            if st != "closed":
                differ("unit_state", key, st, "closed", f"PR #{n} was closed unmerged on GitHub; the store has {key} "
                       f"{st}", pr.get("closed_at"))  # fmt: skip
            continue
        if st == "closed":
            differ("unit_state", key, st, "open", f"PR #{n} is open on GitHub but {key} is closed in the store",
                   pr.get("updated_at"))  # fmt: skip
            continue
        if st in OPEN_STATES:
            checked["pr_head"] += 1
            if unit.get("head") != pr["head"]:
                differ("pr_head", key, unit.get("head"), pr["head"], f"PR #{n}'s head on GitHub isn't the head the "
                       f"store has for {key}", pr.get("updated_at"))  # fmt: skip
            elif pr.get("files") is not None:
                checked["pr_risk"] += 1
                risk = assess([f["path"] for f in pr["files"]], cfg, {f["path"]: f.get("patch") for f in pr["files"]})
                if (unit.get("tier"), bool(unit.get("critical"))) != (risk.tier, risk.critical):
                    differ("pr_risk", key, {"tier": unit.get("tier"), "critical": bool(unit.get("critical"))},
                           {"tier": risk.tier, "critical": risk.critical},
                           f"PR #{n}'s changed paths give another risk tier than the store's")  # fmt: skip

    # GitHub items the store doesn't know
    for n, issue in sorted(issues.items()):
        if issue["state"] == "open" and n != control_issue:
            checked["unknown_to_store"] += 1
            if f"issue-{n}" not in units:
                differ("unknown_to_store", f"issue-{n}", None, "open", f"open issue #{n} has no unit in the store",
                       issue.get("created_at"), issue.get("updated_at"))  # fmt: skip
    for n, pr in sorted(prs.items()):
        if pr["state"] == "open" and not pr["draft"] and not pr["branch"].startswith("loop/") and n not in owned:
            checked["unknown_to_store"] += 1
            if f"pr-{n}" not in units:
                differ("unknown_to_store", f"pr-{n}", None, "open", f"open PR #{n} has no unit in the store",
                       pr.get("created_at"), pr.get("updated_at"))  # fmt: skip

    # main
    gh_main, st_main = (view.get("main") or {}).get("sha"), (state.get("main") or {}).get("sha")
    if gh_main:
        checked["main_sha"] += 1
        if gh_main != st_main:
            cmp = (view.get("main") or {}).get("compare") or {}
            new_at = cmp.get("first_new_at") if cmp.get("status") == "ahead" else None
            differ("main_sha", None, st_main, gh_main, "main on GitHub isn't the sha the store has"
                   + (" (main moved ahead)" if new_at else ""), new_at)  # fmt: skip

    # config
    if config_hash is not None:
        checked["config"] += 1
        if state.get("config_hash") != config_hash:
            differ("config", None, state.get("config_hash"), config_hash, "the project file and runtime overrides "
                   "give another config hash than the last tick recorded (if the file changed since, the next tick "
                   "records it)")  # fmt: skip

    return {"mismatches": mismatches, "pending": pending, "checked": checked,
            "not_compared": {row: v["reason"] for row, v in FACTS.items() if "reason" in v},
            "last_tick_at": state.get("last_tick_at")}  # fmt: skip


def summary(report: Mapping[str, Any]) -> str:
    n = sum(report["checked"].values())
    by: dict[str, int] = {}
    for m in report["mismatches"]:
        by[m["fact"]["check"]] = by.get(m["fact"]["check"], 0) + 1
    parts = ", ".join(f"{k} {v}" for k, v in sorted(by.items())) or "none"
    return (f"{n} facts compared; {len(report['mismatches'])} mismatches ({parts}); {len(report['pending'])} pending "
            f"(GitHub changed after the last tick)")  # fmt: skip


# ------------------------------------------------------------------------------------------------------ the shell
def add_arguments(sub: Any) -> None:
    p = sub.add_parser("reconcile", help="compare the store with GitHub fact by fact (§5.2); reads only")
    p.add_argument("--state", default=None, help="the state repo's clone (default as for `loopctl tick`)")
    p.add_argument("--state-json", default=None, help="a state.json file instead of a clone (dry runs)")
    p.add_argument("--project", default=None, help="the project file (default as for `loopctl tick`)")
    p.add_argument("--view", default=None, help="a saved GitHub view instead of reading GitHub (offline)")
    p.add_argument("--save-view", default=None, help="also write the GitHub view read to this file")
    p.add_argument("--control-issue", type=int, default=None, help="default: install.json's control_issue")
    p.add_argument("--out", default=None, help="write the report here (the file `reconcile-report` reads)")


def run(args: argparse.Namespace, reader_for: Callable[[str], Any] | None = None, out: Any = sys.stdout) -> int:
    from loopctl import commands
    from loopctl.config import ConfigError, resolve
    from loopctl.install import fill_defaults

    fill_defaults(args)
    project = Path(args.project)
    text = project.read_text(encoding="utf-8") if project.exists() else None
    try:
        cfg = resolve(text, project_file=str(project))
        install: Mapping[str, Any] = {}
        if args.state_json:
            state = json.loads(Path(args.state_json).read_text(encoding="utf-8"))
        else:
            from loopctl.controller_cli import _store

            store = _store(args.state, cfg)
            store.sync()
            state = store.read_state()
            install = json.loads(store.read_file("install.json") or "null") or {}
        eff = commands.effective(text, state, str(project))
        if args.view:
            view = json.loads(Path(args.view).read_text(encoding="utf-8"))
        else:
            if reader_for is None:
                from loopctl.github import GitHubReader

                reader_for = GitHubReader
            view = read(reader_for(_get(cfg, "project.repo")), state, _get(cfg, "project.main"))
        if args.save_view:
            Path(args.save_view).write_text(json.dumps(view, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        control = args.control_issue if args.control_issue is not None else install.get("control_issue")
        report = compare(state, view, cfg=eff, control_issue=control, config_hash=eff.hash)
    except (ReconcileError, ConfigError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"loopctl reconcile: {exc}", file=sys.stderr)
        return 1
    report["summary"] = summary(report)
    report["read_at"] = clock.stamp()
    text_out = json.dumps(report, indent=1, ensure_ascii=False) + "\n"
    if args.out:
        Path(args.out).write_text(text_out, encoding="utf-8")
    out.write(report["summary"] + "\n" if args.out else text_out)
    return 0
