"""`loopctl compare-v4 --since <time>`: v2's decision log against what v4 actually did (M2's exit: "every decision that
differs from v4's explained", proof-plan K-RECON). Reads only.

Two inputs:
- **v2**: the `tick.done` decision logs in the state repo (`loopctl/decisions.py`): per tick, per unit, what v2 started
  or would have started (shadow) or refused, and the rule.
- **v4**: its actions, from ensemble's `loop/state` history (each `claims/<name>.md` lifetime with its `lane:`) and
  GitHub (each merged PR), read through the read-only client (`GitHubReader`: `GET` only). The same shape as the
  T-REPLAY fixture (`docs/v2/data/extract_replay.py`), so `--v4-file` can take a saved extract instead.

Pairing, per unit and kind (`v4 lane fix` = v2 `fix` on `issue-N`; `review` = `review` on `pr-N`; a merge of PR N =
v2's `gate` on PR N, whose unit is `pr-N` or an issue unit carrying `pr: N`):
- **v4 acted, v2 agreed**: some tick from `BEFORE` before to `AFTER` after the action would start the same kind on the
  unit (v2 in shadow doesn't see v4's claims, so a tick shortly after still counts; the lag is reported).
- **v4 acted, v2 didn't** (a difference): v2's own entry for the unit at the last tick before the action (else the first
  after) gives the decision, the rule and the setting that would change it; no entry at all is explained by what the
  log can say (the unit wasn't open in v2's store at that tick, or it fell among the entries the tick didn't record).
- **v2 would have, v4 didn't** (a difference): a run of consecutive ticks wanting the same (unit, kind) with no v4
  action on it from `BEFORE` before the run's first tick to `AFTER` after its last; v2's rule is `admitted` and its why.
- **No v2 tick near a v4 action** (coverage): the controller wasn't ticking; nothing to compare.
- **Not comparable**: v4 lanes with no v2 unit (`ship` is one lock for all merges and is replaced by the merge itself;
  `watch`, scans and unnamed lanes) and v2 kinds v4 records no claim for (`triage`, `reconcile`, `check`, `revert`):
  counted, not listed as differences.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loopctl import clock, decisions
from loopctl.risk import _get

BEFORE = timedelta(hours=2)
AFTER = timedelta(hours=1)
LANE_KIND = {"fix": "fix", "review": "review"}  # v4 lane -> v2 kind (merges come from GitHub)
COMPARABLE = ("fix", "review", "gate")
WANTS = ("start", "would_start")
V4_REF = "loop/state"
CLAIM_LOOKBACK = timedelta(days=2)  # claims opened this long before --since are read too (still running at --since)
PAGE = 100
MAX_PAGES = 50
LANE = re.compile(r"^lane:\s*(\S+)", re.M)
RULE_TEXT = {
    "shadow": "v2 would have started it (run.mode decides only)",
    "admitted": "v2 started it",
    "paused": "paused (run.paused / run.paused_kinds)",
    "stop": "a STOP line",
    "quiet_hours": "quiet hours (schedule.night)",
    "kind": "the kind isn't in work.kinds",
    "platform_limit": "a platform usage limit",
    "main_red": "main was red (merges stop until it is green)",
    "scope": "out of scope (scope.excluded_labels / excluded_paths)",
    "capacity": "capacity full (capacity.max_sessions)",
    "budget_window": "the 5-hour window's budget room (budget.window)",
    "budget_week": "the week's budget room (budget.week)",
    "round_cap": "the review round cap",
    "refused": "the reducer refused the lease",
    "brief": "the brief needed the owner",
    "needs_owner": "waiting for the owner",
    "attempts": "out of attempts",
    "stalled": "its last worker ended without progress",
    "not_listed": "work.scope = listed and work.order doesn't name it",
    "below_min_priority": "below work.min_priority",
    "not_eligible": "not a candidate in its state",
}


class CompareError(RuntimeError):
    """An input can't be read as asked."""


@dataclass(frozen=True)
class V4Action:
    unit: str  # issue-N or pr-N
    kind: str  # fix | review | gate
    start: datetime
    end: datetime | None  # None: still running at the end of the record, or a point (a merge)
    source: str  # "claim <name> (lane <lane>)" or "merged PR #N"

    @property
    def pr(self) -> int | None:
        return int(self.unit[3:]) if self.unit.startswith("pr-") else None


@dataclass
class Report:
    since: str
    until: str
    v2_ticks: int = 0
    v2_first_tick: str | None = None
    v2_last_tick: str | None = None
    v4_actions: int = 0
    agreed: list[dict[str, Any]] = field(default_factory=list)
    differences: list[dict[str, Any]] = field(default_factory=list)
    no_v2_tick: list[dict[str, Any]] = field(default_factory=list)
    not_comparable: dict[str, dict[str, int]] = field(default_factory=lambda: {"v4": {}, "v2": {}})

    def as_dict(self) -> dict[str, Any]:
        by: dict[str, int] = {}
        for d in self.differences:
            key = f"{d['direction']}:{d['v2']['rule'] if d['v2'] else 'no_entry'}"
            by[key] = by.get(key, 0) + 1
        return {"since": self.since, "until": self.until,
                "v2": {"ticks": self.v2_ticks, "first_tick": self.v2_first_tick, "last_tick": self.v2_last_tick},
                "v4": {"actions": self.v4_actions},
                "summary": {"agreed": len(self.agreed), "differences": len(self.differences),
                            "no_v2_tick": len(self.no_v2_tick), "differences_by_rule": dict(sorted(by.items()))},
                "differences": self.differences, "no_v2_tick": self.no_v2_tick, "agreed": self.agreed,
                "not_comparable": self.not_comparable}  # fmt: skip


# ------------------------------------------------------------------------------------------------------- v4's side
def _ts(text: str | None) -> datetime | None:
    if not text:
        return None
    t = clock.parse(text)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def claim_unit(name: str) -> str | None:
    """v4's claim file name -> v2's unit key: `5` and `issue-5` are issue-5, `pr-7` is pr-7; others have none."""
    if name.isdigit():
        return f"issue-{int(name)}"
    m = re.fullmatch(r"(issue|pr)-(\d{1,8})", name)
    return f"{m.group(1)}-{int(m.group(2))}" if m else None


def v4_actions(data: Mapping[str, Any], since: datetime, until: datetime) -> tuple[list[V4Action], dict[str, int]]:
    """v4's comparable actions in [since, until] from a replay-shaped record, and the other claims counted by lane."""
    out: list[V4Action] = []
    other: dict[str, int] = {}
    head = data.get("claim") or ["name", "lane", "start", "end"]
    for row in data.get("claims", []):
        c = dict(zip(head, row))
        start = _ts(c["start"])
        if start is None or not since <= start <= until:
            continue
        unit, kind = claim_unit(str(c["name"])), LANE_KIND.get(str(c["lane"]))
        if unit is None or kind is None:
            lane = str(c["lane"]) or "(none)"
            other[f"claim lane {lane}"] = other.get(f"claim lane {lane}", 0) + 1
            continue
        out.append(V4Action(unit, kind, start, _ts(c.get("end")), f"claim {c['name']} (lane {c['lane']})"))
    head = data.get("pr") or ["number", "created", "merged", "closed"]
    for row in data.get("prs", []):
        p = dict(zip(head, row))
        merged = _ts(p.get("merged"))
        if merged is not None and since <= merged <= until:
            out.append(V4Action(f"pr-{int(p['number'])}", "gate", merged, None, f"merged PR #{int(p['number'])}"))
    return sorted(out, key=lambda a: (a.start, a.unit, a.kind)), other


def _pages(reader: Any, path: str, params: dict[str, Any]) -> Iterable[Any]:
    for page in range(1, MAX_PAGES + 1):
        r = reader.get(path, {**params, "per_page": PAGE, "page": page})
        if r.status != 200 or not isinstance(r.data, list):
            raise CompareError(f"GET {path} answered {r.status}")
        yield from r.data
        if len(r.data) < PAGE:
            return
    raise CompareError(f"GET {path}: more than {MAX_PAGES} pages; narrow --since")


def read_v4(reader: Any, since: datetime, until: datetime, ref: str = V4_REF) -> dict[str, Any]:
    """v4's record from GitHub, read only: claim lifetimes from `ref`'s commits touching `claims/` (each added file's
    `lane:` read at the commit that added it) and every PR merged in the window. Replay-fixture shaped."""
    stamp = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    commits = list(_pages(reader, "/commits", {"sha": ref, "path": "claims", "since": stamp(since - CLAIM_LOOKBACK),
                                               "until": stamp(until)}))  # fmt: skip
    open_: dict[str, list[Any]] = {}
    done: list[list[Any]] = []
    for c in sorted(commits, key=lambda c: (c["commit"]["committer"]["date"], c["sha"])):
        at = c["commit"]["committer"]["date"]
        detail = reader.get(f"/commits/{c['sha']}")
        if detail.status != 200:
            raise CompareError(f"GET /commits/{c['sha'][:12]} answered {detail.status}")
        for f in detail.data.get("files") or []:
            path, status = f.get("filename", ""), f.get("status")
            if not (path.startswith("claims/") and path.endswith(".md")):
                continue
            name = path.removeprefix("claims/").removesuffix(".md")
            if status == "added" and name not in open_:
                lane = ""
                body = reader.get(f"/contents/{path}", {"ref": c["sha"]})
                if body.status == 200 and isinstance(body.data, dict) and body.data.get("content"):
                    text = base64.b64decode(body.data["content"]).decode("utf-8", "replace")
                    lane = m.group(1) if (m := LANE.search(text)) else ""
                open_[name] = [name, lane, at, None]
            elif status == "removed" and name in open_:
                row = open_.pop(name)
                row[3] = at
                done.append(row)
    done.extend(open_.values())
    prs = []
    for p in _pages(reader, "/pulls", {"state": "closed", "sort": "updated", "direction": "desc"}):
        if _ts(p["updated_at"]) < since:
            break
        if p.get("merged_at"):
            prs.append([p["number"], p["created_at"], p["merged_at"], p.get("closed_at")])
    return {"source": f"{ref} + GitHub REST (read at {clock.stamp()})", "claim": ["name", "lane", "start", "end"],
            "claims": sorted(done, key=lambda r: r[2]), "pr": ["number", "created", "merged", "closed"],
            "prs": sorted(prs)}  # fmt: skip


# -------------------------------------------------------------------------------------------------------- pairing
def _same(entry: Mapping[str, Any], unit: str) -> bool:
    if entry["unit"] == unit:
        return True
    return unit.startswith("pr-") and entry.get("pr") == int(unit[3:])


def _entry_of(tick: Mapping[str, Any], unit: str) -> dict[str, Any] | None:
    found = [e for e in tick["decided"] if _same(e, unit)]
    return next((e for e in found if e["decision"] in WANTS), found[0] if found else None)


def _v2(entry: Mapping[str, Any] | None, tick: Mapping[str, Any]) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {"tick_at": tick["at"], "tick_id": tick["tick_id"], "unit": entry["unit"], "kind": entry["kind"],
            "decision": entry["decision"], "rule": entry["rule"], "why": entry.get("why"),
            "setting": entry.get("setting")}  # fmt: skip


def _explain(direction: str, kind: str, v2: Mapping[str, Any] | None, tick: Mapping[str, Any] | None) -> str:
    if direction == "v2_only":
        return (f"v2 wanted to start {kind} ({v2['why'] or v2['rule']}); v4 started no {kind} on it in that time"
                if v2 else f"v2 wanted to start {kind}; v4 didn't")  # fmt: skip
    if v2 is None:
        if tick and tick["omitted"]:
            return (f"no entry at v2's tick {tick['at']}: the unit may be among the {tick['omitted']} entries that "
                    "tick didn't record (decisions.MAX_ENTRIES)")  # fmt: skip
        return (f"no entry at v2's tick {tick['at'] if tick else '-'}: the unit wasn't open in v2's store then (not "
                "seen yet, already closed or merged, or holding a lease)")  # fmt: skip
    if v2["kind"] != kind and v2["decision"] in WANTS:
        return f"v2 would have done {v2['kind']} on it, not {kind} (its own view of the unit's state)"
    why = RULE_TEXT.get(v2["rule"], v2["rule"])
    detail = f": {v2['why']}" if v2.get("why") else ""
    change = f"; the setting that would change it: {v2['setting']}" if v2.get("setting") else ""
    return f"v2 {v2['decision'].replace('_', ' ')} {v2['kind']}: {why}{detail}{change}"


def _end(a: V4Action, until: datetime) -> datetime:
    """When v4's action stopped: a claim's removal (an open claim runs to the end of the window), a merge its moment."""
    return a.end or (a.start if a.kind == "gate" else until + AFTER)


def compare(ticks: Sequence[Mapping[str, Any]], v4: Mapping[str, Any], since: datetime, until: datetime) -> Report:
    """Pure: v2's recorded ticks (decisions.ticks) and v4's record (replay shape) -> the report."""
    ticks = [t for t in ticks if since - BEFORE <= _ts(t["at"]) <= until + AFTER]
    at = [_ts(t["at"]) for t in ticks]
    report = Report(clock.stamp_at(since), clock.stamp_at(until))
    inside = [t for t, a in zip(ticks, at) if since <= a <= until]
    report.v2_ticks = len(inside)
    report.v2_first_tick = inside[0]["at"] if inside else None
    report.v2_last_tick = inside[-1]["at"] if inside else None
    actions, other = v4_actions(v4, since, until)
    # for "v2 would have, v4 didn't": also v4's claims opened before --since and still running in the window
    around, _ = v4_actions(v4, since - CLAIM_LOOKBACK - BEFORE, until + AFTER)
    report.v4_actions = len(actions)
    report.not_comparable["v4"] = dict(sorted(other.items()))

    for a in actions:
        near = [(t, x) for t, x in zip(ticks, at) if a.start - BEFORE <= x <= a.start + AFTER]
        row = {"unit": a.unit, "kind": a.kind, "at": clock.stamp_at(a.start), "v4": a.source}
        if not near:
            report.no_v2_tick.append(dict(row, explanation=f"no v2 tick from {BEFORE} before to {AFTER} after"))
            continue
        agree = next(((t, x, e) for t, x in near if (e := _entry_of(t, a.unit)) and e["decision"] in WANTS
                      and e["kind"] == a.kind), None)  # fmt: skip
        if agree:
            t, x, e = agree
            lag = int((x - a.start).total_seconds() // 60)
            report.agreed.append(dict(row, v2=_v2(e, t), lag_minutes=lag))
            continue
        before = [(t, x) for t, x in near if x <= a.start]
        t = (before[-1] if before else near[0])[0]
        e = _entry_of(t, a.unit)
        v2 = _v2(e, t)
        report.differences.append(dict(row, direction="v4_only", v2=v2, explanation=_explain("v4_only", a.kind, v2, t)))

    # v2 wanted (unit, kind) over a run of consecutive ticks; did v4 act on it around that run?
    runs: dict[tuple[str, str], list[Any]] = {}
    finished: list[tuple[tuple[str, str], list[Any]]] = []
    for t, x in zip(inside, [_ts(t["at"]) for t in inside]):
        wanted = {}
        for e in t["decided"]:
            if e["decision"] in WANTS:
                wanted[(e["unit"], e["kind"])] = e
        for key in list(runs):
            if key not in wanted:
                finished.append((key, runs.pop(key)))
        for key, e in wanted.items():
            if key in runs:
                runs[key][1], runs[key][3] = x, runs[key][3] + 1
            else:
                runs[key] = [x, x, (t, e), 1]
    finished.extend(runs.items())
    v2_other: dict[str, int] = {}
    for (unit, kind), (first, last, (t, e), n) in sorted(finished, key=lambda r: (r[1][0], r[0])):
        if kind not in COMPARABLE:
            v2_other[f"would start {kind}"] = v2_other.get(f"would start {kind}", 0) + 1
            continue
        hit = any(a.kind == kind and (a.unit == unit or (a.pr is not None and e.get("pr") == a.pr))
                  and a.start <= last + AFTER and _end(a, until) >= first - BEFORE for a in around)  # fmt: skip
        if hit:
            continue
        v2 = _v2(e, t)
        report.differences.append({"unit": unit, "kind": kind, "at": clock.stamp_at(first), "until": clock.stamp_at(last),
                                   "ticks": n, "v4": None, "direction": "v2_only", "v2": v2,
                                   "explanation": _explain("v2_only", kind, v2, t)})  # fmt: skip
    report.not_comparable["v2"] = dict(sorted(v2_other.items()))
    report.differences.sort(key=lambda d: (d["at"], d["unit"], d["kind"]))
    return report


def text(doc: Mapping[str, Any]) -> str:
    s = doc["summary"]
    out = [f"compare-v4 {doc['since']} .. {doc['until']}",
           f"v2: {doc['v2']['ticks']} recorded ticks ({doc['v2']['first_tick'] or '-'} .. {doc['v2']['last_tick'] or '-'}); "
           f"v4: {doc['v4']['actions']} comparable actions",
           f"agreed {s['agreed']}, differences {s['differences']}, v4 actions with no v2 tick near {s['no_v2_tick']}"]
    if s["differences_by_rule"]:
        out.append("differences by rule: " + ", ".join(f"{k} {v}" for k, v in s["differences_by_rule"].items()))
    out.append("\nDifferences (each with v2's stated rule):")
    for d in doc["differences"]:
        if d["direction"] == "v4_only":
            out.append(f"- {d['at']} v4 {d['kind']} on {d['unit']} ({d['v4']}); v2 didn't: {d['explanation']}")
        else:
            out.append(f"- {d['at']}..{d['until']} v2 would {d['kind']} {d['unit']} ({d['ticks']} ticks); v4 didn't: "
                       f"{d['explanation']}")  # fmt: skip
    if not doc["differences"]:
        out.append("- none")
    if doc["no_v2_tick"]:
        out.append("\nv4 actions with no v2 tick near (controller not ticking: not compared):")
        out += [f"- {d['at']} v4 {d['kind']} on {d['unit']} ({d['v4']})" for d in doc["no_v2_tick"]]
    nc = doc["not_comparable"]
    if nc["v4"] or nc["v2"]:
        out.append("\nNot comparable (counted only): "
                   + ", ".join(f"v4 {k} {v}" for k, v in nc["v4"].items())
                   + ("; " if nc["v4"] and nc["v2"] else "") + ", ".join(f"v2 {k} {v}" for k, v in nc["v2"].items()))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------------------------------ the shell
def add_arguments(sub: Any) -> None:
    p = sub.add_parser("compare-v4", help="v2's decision log against v4's actions (M2's exit); reads only")
    p.add_argument("--since", required=True, help="start of the window (ISO time, e.g. 2026-11-20T00:00Z)")
    p.add_argument("--until", default=None, help="end of the window (default: now)")
    p.add_argument("--state", default=None, help="the state repo's clone (default as for `loopctl status`)")
    p.add_argument("--project", default=None, help="the project file (default as for `loopctl status`)")
    p.add_argument("--events", default=None, help="a directory holding events/ instead of the state clone (offline)")
    p.add_argument("--v4-file", default=None, help="v4's record as JSON (replay-fixture shape) instead of GitHub")
    p.add_argument("--v4-ref", default=V4_REF, help="v4's state branch in the target (default: %(default)s)")
    p.add_argument("--save-v4", default=None, help="also write v4's record read from GitHub to this file")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.add_argument("--out", default=None, help="also write the JSON report here")


def _time(text: str) -> datetime:
    t = text.strip()
    if re.fullmatch(r"\d{4}-\d\d-\d\d", t):
        t += "T00:00:00"
    t = re.sub(r"T(\d\d:\d\d)Z?$", r"T\1:00", t)
    parsed = datetime.fromisoformat(t.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _days(since: datetime, until: datetime) -> list[str]:
    day, out = (since - BEFORE).date(), []
    while day <= (until + AFTER).date():
        out.append(day.strftime("%Y/%m/%d"))
        day += timedelta(days=1)
    return out


def run(args: argparse.Namespace, reader_for: Callable[[str], Any] | None = None, out: Any = sys.stdout) -> int:
    from loopctl.config import ConfigError, resolve
    from loopctl.store import read_event_files

    try:
        since = _time(args.since)
        until = _time(args.until) if args.until else clock.now()
        if until <= since:
            raise CompareError("--until must be after --since")
        target = None
        if args.events:
            root = Path(args.events)
        else:
            from loopctl.controller_cli import _store
            from loopctl.install import fill_defaults

            fill_defaults(args)
            project = Path(args.project)
            cfg = resolve(project.read_text(encoding="utf-8") if project.exists() else None, project_file=str(project))
            target = _get(cfg, "project.repo")
            store = _store(args.state, cfg)
            store.sync()
            root = store.path
        ticks = decisions.ticks(read_event_files(root, _days(since, until)))
        if args.v4_file:
            v4 = json.loads(Path(args.v4_file).read_text(encoding="utf-8"))
        else:
            if target is None:
                raise CompareError("reading v4 from GitHub needs the project file (--project); or pass --v4-file")
            if reader_for is None:
                from loopctl.github import GitHubReader

                reader_for = GitHubReader
            v4 = read_v4(reader_for(target), since, until, args.v4_ref)
            if args.save_v4:
                Path(args.save_v4).write_text(json.dumps(v4, indent=1) + "\n", encoding="utf-8")
        doc = compare(ticks, v4, since, until).as_dict()
    except (CompareError, ConfigError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"loopctl compare-v4: {exc}", file=sys.stderr)
        return 1
    doc["read_at"] = clock.stamp()
    if args.out:
        Path(args.out).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    out.write(json.dumps(doc, indent=1, ensure_ascii=False) + "\n" if args.json else text(doc))
    return 0
