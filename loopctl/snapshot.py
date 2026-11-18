"""The tick's GitHub snapshot (phase2-design §4.1 step 2), read through the read-only client (decision record 0028 D).

`read()` makes a plain JSON document of what the tick needs from the target: open issues, open and recently closed
PRs (with changed paths and patches for the PRs whose risk must be (re)computed), branches and main's sha. It runs
only `GitHubReader.get` (T-INV-11 a). `ingest()` turns the snapshot into events, purely:

- an open issue that isn't a unit yet -> `issue.seen`, except `install.json`'s `control_issue` (the loop's own
  channel, never a work item);
- an open, non-draft PR that no unit owns and whose branch isn't one of the loop's own (`loop/…`, linked by
  `fix.pushed`) -> `pr.seen` (adopted for review);
- a unit's PR whose head moved outside the loop -> `pr.head`; a unit without a tier, or whose head moved -> `pr.risk`
  from the changed paths (risk.assess);
- a unit's PR merged on GitHub -> `merge.done` with the merged commit's tree (the reducer records `merged` only if
  the preconditions held and that tree is the one the gate tested, I3); closed unmerged -> `unit.closed`
  (reason `pr_closed`);
- main's sha moved -> `main.moved` (the post-merge check of §10.6 runs on it);
- an issue unit not yet worked on whose issue is no longer open (only when the issue list was read completely)
  -> `unit.closed` (reason `issue_closed`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from loopctl.risk import assess
from loopctl.store import make_event

PAGES = 10
PER_PAGE = 100
LOOP_BRANCH = "loop/"
CLOSED_PRS = 30
_NEXT = re.compile(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="next"')
UNWORKED = ("new", "triaged", "queued")


class SnapshotError(RuntimeError):
    """GitHub couldn't be read: the tick decides nothing from a partial picture."""


def _pages(reader: Any, path: str, params: Mapping[str, Any]) -> tuple[list[Any], bool]:
    """Every item of a paginated list (up to PAGES pages) and whether the list was read to its end."""
    out: list[Any] = []
    page = 1
    for _ in range(PAGES):
        reply = reader.get(path, {**params, "per_page": PER_PAGE, "page": page})
        if reply.status != 200 or not isinstance(reply.data, list):
            raise SnapshotError(f"GET {path} answered {reply.status}")
        out += reply.data
        link = {k.lower(): v for k, v in reply.headers.items()}.get("link", "")
        nxt = _NEXT.search(link)
        if not nxt:
            return out, True
        page = int(nxt.group(1))
    return out, False


def _pr(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"number": raw["number"], "title": raw.get("title") or "", "head": raw["head"]["sha"],
            "branch": raw["head"].get("ref") or "", "draft": bool(raw.get("draft")),
            "state": raw.get("state", "open"), "merged": bool(raw.get("merged_at")),
            "merged_sha": raw.get("merge_commit_sha") if raw.get("merged_at") else None,
            "labels": sorted(lb["name"] for lb in raw.get("labels") or []), "files": None}  # fmt: skip


def read(reader: Any, state: Mapping[str, Any], main: str = "main") -> dict[str, Any]:
    """The snapshot. PR files (with patches) are read only for PRs whose risk must be computed this tick."""
    issues, complete = _pages(reader, "/issues", {"state": "open"})
    prs_open, _ = _pages(reader, "/pulls", {"state": "open"})
    closed = reader.get("/pulls", {"state": "closed", "sort": "updated", "direction": "desc", "per_page": CLOSED_PRS})
    if closed.status != 200:
        raise SnapshotError(f"GET /pulls?state=closed answered {closed.status}")
    branches, _ = _pages(reader, "/branches", {})
    head = reader.get(f"/commits/{main}")
    snap: dict[str, Any] = {
        "main_sha": head.data.get("sha") if head.status == 200 and isinstance(head.data, dict) else None,
        "issues": [{"number": i["number"], "title": i.get("title") or "",
                    "labels": sorted(lb["name"] for lb in i.get("labels") or [])}
                   for i in issues if "pull_request" not in i],  # fmt: skip
        "issues_complete": complete,
        "prs": [_pr(p) for p in prs_open] + [_pr(p) for p in closed.data or [] if p.get("state") == "closed"],
        "branches": sorted(b["name"] for b in branches),
    }
    by_pr = {u["pr"]: u for u in state["units"].values() if u.get("pr") is not None}
    for pr in snap["prs"]:
        unit = by_pr.get(pr["number"])
        if pr["merged"] and pr["merged_sha"] and unit is not None and unit["state"] not in ("merged", "closed"):
            commit = reader.get(f"/commits/{pr['merged_sha']}")
            if commit.status == 200 and isinstance(commit.data, dict):
                pr["merged_tree"] = ((commit.data.get("commit") or {}).get("tree") or {}).get("sha")
        if pr["state"] == "open" and (unit is None or unit.get("tier") is None or unit.get("head") != pr["head"]):
            files, _ = _pages(reader, f"/pulls/{pr['number']}/files", {})
            pr["files"] = [{"path": f["filename"], "patch": f.get("patch")} for f in files]
    return snap


def adoptable(pr: Mapping[str, Any]) -> bool:
    return pr["state"] == "open" and not pr["draft"] and not pr["branch"].startswith(LOOP_BRANCH)


def ingest(state: Mapping[str, Any], snap: Mapping[str, Any], *, epoch: int, cfg: Any,
           control_issue: int | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """Events from the snapshot (pure). Units are created before anything refers to them. `control_issue`
    (install.json) is skipped explicitly: it never becomes a unit, whatever its labels."""
    events: list[dict[str, Any]] = []
    units = state["units"]

    def ev(type_: str, payload: dict[str, Any], unit: str) -> None:
        events.append(make_event(type_, payload, writer="controller", unit=unit, epoch=epoch))

    open_issues = {i["number"] for i in snap["issues"]}
    for issue in sorted(snap["issues"], key=lambda i: i["number"]):
        if issue["number"] == control_issue:
            continue
        if f"issue-{issue['number']}" not in units:
            ev("issue.seen", {"issue": issue["number"]}, f"issue-{issue['number']}")
    by_pr = {u["pr"]: k for k, u in units.items() if u.get("pr") is not None}
    for pr in sorted(snap["prs"], key=lambda p: p["number"]):
        key = by_pr.get(pr["number"])
        if key is None:
            if adoptable(pr) and f"pr-{pr['number']}" not in units:
                ev("pr.seen", {"pr": pr["number"], "head": pr["head"]}, f"pr-{pr['number']}")
                _risk(ev, f"pr-{pr['number']}", pr, cfg)
            continue
        unit = units[key]
        if unit["state"] in ("merged", "closed"):
            continue
        if pr["state"] == "closed":
            if pr["merged"] and unit["state"] not in UNWORKED:
                tested = (unit.get("gate") or {}).get("tested") or pr["merged_sha"]
                payload = {"pr": pr["number"], "merged_sha": pr["merged_sha"], "tested": tested}
                if pr.get("merged_tree"):
                    payload["merged_tree"] = pr["merged_tree"]
                ev("merge.done", payload, key)
            elif not pr["merged"]:
                ev("unit.closed", {"reason": "pr_closed"}, key)  # the PR was closed on GitHub without a merge
            continue
        moved = pr["head"] != unit["head"] and unit["state"] in ("in_review", "changes", "approved", "gating")
        if moved:
            ev("pr.head", {"pr": pr["number"], "head": pr["head"]}, key)
        if moved or (unit.get("tier") is None and pr["head"] == unit["head"]):
            _risk(ev, key, pr, cfg)
    main = snap.get("main_sha")
    if main and main != state["main"]["sha"]:
        ev("main.moved", {"sha": main}, f"check-{main[:12]}")
    if snap.get("issues_complete"):
        for key, unit in sorted(units.items()):
            if key.startswith("issue-") and unit["state"] in UNWORKED and not unit.get("lease"):
                if int(key.split("-")[1]) not in open_issues:
                    ev("unit.closed", {"reason": "issue_closed"}, key)  # the issue is no longer open on GitHub
    return events


def _risk(ev: Any, key: str, pr: Mapping[str, Any], cfg: Any) -> None:
    if pr.get("files") is None:
        return
    paths = [f["path"] for f in pr["files"]]
    risk = assess(paths, cfg, {f["path"]: f.get("patch") for f in pr["files"]})
    ev("pr.risk", {"head": pr["head"], "tier": risk.tier, "critical": risk.critical, "gate_files": risk.gate_files,
                   "changed": paths[:300]}, key)  # fmt: skip


def scope(snap: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, dict[str, list[str]]]:
    """unit key -> {labels, paths} for admission's scope rule (I9)."""
    out: dict[str, dict[str, list[str]]] = {}
    for issue in snap["issues"]:
        out[f"issue-{issue['number']}"] = {"labels": list(issue["labels"]), "paths": []}
    by_pr = {u["pr"]: k for k, u in state["units"].items() if u.get("pr") is not None}
    for pr in snap["prs"]:
        key = by_pr.get(pr["number"], f"pr-{pr['number']}")
        paths = [f["path"] for f in pr["files"]] if pr.get("files") is not None else list(
            state["units"].get(key, {}).get("changed", []))
        base = out.setdefault(key, {"labels": [], "paths": []})
        base["labels"] = sorted(set(base["labels"]) | set(pr["labels"]))
        base["paths"] = paths
    return out


def titles(snap: Mapping[str, Any], state: Mapping[str, Any]) -> dict[str, str]:
    out = {f"issue-{i['number']}": i["title"] for i in snap.get("issues", [])}
    by_pr = {u["pr"]: k for k, u in state["units"].items() if u.get("pr") is not None}
    for pr in snap.get("prs", []):
        out.setdefault(by_pr.get(pr["number"], f"pr-{pr['number']}"), pr["title"])
    return out
