"""`loopctl status [--json]`: the one status source (phase2-design §12.1; the shape is fixed by page/README.md and
page/fixtures/status-busy.json).

`build()` is pure: state, config, the GitHub snapshot, the `STOP` file, recent events and the clock in, the status
document out. It answers the ten needs: (1) what runs, (2) what waits for the owner, (3) for every idle slot the
rule that holds it and the setting that would change it, (4) the queue in pick order, (5) throughput, (6) spend,
(7) recent stops, errors and limits, (8) each unit's state and history, (9) PRs and branches, (10) the next tick.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from loopctl import admission, budget, clock, leases, stop
from loopctl.risk import _get
from loopctl.schedule import candidates

VERSION = 1
SWEEP_MINUTES = 60
RECENT_MAX = 20
HISTORY_MAX = 10
UNIT_DAYS = 7
ROLE_OF_KIND = {"fix": "fix", "review": "review", "gate": "gate", "triage": "triage", "reconcile": "reconcile",
                "scan": "fix", "build": "fix", "check": "check", "revert": "revert"}  # fmt: skip
MODEL_ROLE = {"reconcile": "triage", "check": "gate", "revert": "gate"}  # roles without their own models.* keys (= tick's)
QUESTION_TITLES = {
    "cap_reached": "{unit} used its {cap} review rounds and still has a blocking finding",
    "critical": "{unit} changes a critical path and needs your approval",
    "attempts": "{unit} failed three times",
    "conflict": "{unit} has a conflict the loop can't resolve",
    "main_red": "main is red",
    "controller": "The controller needs you",
    "budget": "The budget needs a decision",
    "other": "A decision is waiting",
}
EVENT_TEXT = {
    "issue.seen": "Issue seen", "pr.seen": "PR adopted for review", "triage.done": "Triaged",
    "lease.granted": "Leased", "lease.bound": "Worker started", "lease.released": "Worker finished",
    "lease.expired": "Lease expired", "fix.pushed": "Fix pushed", "review.submitted": "Review submitted",
    "finding.fixed": "Finding fixed", "finding.reopened": "Finding reopened", "finding.waived": "Finding waived",
    "gate.passed": "Gate passed", "gate.failed": "Gate failed", "merge.requested": "Merge requested",
    "merge.done": "Merged on GitHub", "question.opened": "Asked the owner", "question.answered": "Owner answered",
    "unit.closed": "Closed", "pr.head": "New commits on the PR", "pr.risk": "Risk assessed",
    "reconcile.due": "Reconcile scheduled", "reconcile.reported": "Reconcile reported",
}  # fmt: skip


def recent_days(now: datetime | None = None, days: int = UNIT_DAYS + 1) -> list[str]:
    now = now or clock.now()
    return [(now - timedelta(days=d)).strftime("%Y/%m/%d") for d in range(days)]


def _url(repo: str, unit: Mapping[str, Any]) -> str | None:
    if not repo:
        return None
    if unit.get("pr") is not None:
        return f"https://github.com/{repo}/pull/{unit['pr']}"
    if unit["key"].startswith("issue-"):
        return f"https://github.com/{repo}/issues/{unit['key'].split('-')[1]}"
    return None


def _title(key: str, titles: Mapping[str, str]) -> str:
    return titles.get(key) or key


def _stop_lines(text: str | None, authors: Mapping[str, str] | None) -> dict[str, Any]:
    lines = []
    for line in stop.parse(text):
        item: dict[str, Any] = {"line": line.line, "by": (authors or {}).get(line.line, "unknown")}
        if line.kinds:
            item["kinds"] = list(line.kinds)
        lines.append(item)
    return {"exists": text is not None, "lines": lines}


def _history(events: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.get("invalid") or not e.get("unit") or e["type"] not in EVENT_TEXT:
            continue
        text = EVENT_TEXT[e["type"]]
        p = e["payload"]
        if e["type"] == "lease.granted":
            text = f"{p['kind'].capitalize()} leased (attempt {p['attempt']})"
        elif e["type"] == "fix.pushed":
            text = f"Fix pushed to PR #{p['pr']}"
        elif e["type"] == "review.submitted":
            text = f"Review round {p['round']}: {len(p['findings'])} finding(s)"
        elif e["type"] in ("unit.closed", "lease.released"):
            text += f": {p['reason']}"
        out.setdefault(e["unit"], []).append({"at": e["at"], "type": e["type"], "text": text[:200]})
    return out


def _recent(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        if e.get("invalid"):
            continue
        t, p = e["type"], e["payload"]
        if t == "limit.observed" and p["status"] != "allowed":
            out.append({"at": e["at"], "type": "limit", "unit": None,
                        "text": f"Platform {p['window'].replace('_', '-')} limit: {p['status'].replace('_', ' ')}."})
        elif t == "lease.expired":
            out.append({"at": e["at"], "type": "stop", "unit": e["unit"],
                        "text": f"The worker on {e['unit']} stopped without finishing (attempt {p['attempt']})."})
        elif t == "gate.failed":
            out.append({"at": e["at"], "type": "error", "unit": e["unit"], "text": f"Gate failed on {e['unit']}."})
        elif t == "command.rejected":
            out.append({"at": e["at"], "type": "error", "unit": None,
                        "text": f"Command {p['command_id']} rejected: {p['reason']}"[:200]})
        elif t == "controller.alert":
            out.append({"at": e["at"], "type": "error", "unit": None, "text": p["problem"][:200]})
        elif t == "lease.released" and p["reason"] == "stopped":
            out.append({"at": e["at"], "type": "stop", "unit": e["unit"], "text": f"{e['unit']} stopped (STOP)."})
    return sorted(out, key=lambda r: r["at"], reverse=True)[:RECENT_MAX]


def _throughput(events: Sequence[Mapping[str, Any]], state: Mapping[str, Any], since: datetime) -> dict[str, int]:
    merged = done = closed = 0
    for e in events:
        if e.get("invalid") or clock.parse(e["at"]) < since:
            continue
        if e["type"] == "merge.done" and state["units"].get(e["unit"] or "", {}).get("state") == "merged":
            merged += 1
        elif e["type"] == "lease.released" and e["payload"]["reason"] == "finished":
            done += 1
        elif e["type"] == "reconcile.reported":
            done += 1
        elif e["type"] == "unit.closed":
            closed += 1
    return {"merged": merged, "units_done": done, "closed": closed}


def _idle(plan: admission.Plan | None, cap: int, busy: int) -> list[dict[str, Any]]:
    free = max(cap - busy, 0)
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, str]] = set()
    for d in plan.decisions if plan else []:
        if d.admitted or (d.kind, d.rule) in seen or len(out) >= free:
            continue
        seen.add((d.kind, d.rule))
        change = None
        if d.key == "STOP":
            change = "remove the line with a confirmed resume"
        elif d.key:
            change = f"change {d.key} (now {d.value!r})" if d.value is not None else f"change {d.key}"
        out.append({"kind": d.kind, "rule": d.key or "rule", "why": f"{d.unit}: {d.rule}"[:300], "setting": d.key,
                    "change": change})  # fmt: skip
    if len(out) < free:
        out.append({"kind": None, "rule": "queue", "why": "Nothing else is waiting to start.", "setting": None,
                    "change": None})  # fmt: skip
    return out


def build(state: Mapping[str, Any], cfg: Any, *, snapshot: Mapping[str, Any] | None, stop_text: str | None,
          install: Mapping[str, Any] | None, tick_id: str | None, events: Sequence[Mapping[str, Any]] = (),
          plan: admission.Plan | None = None, stop_authors: Mapping[str, str] | None = None,
          now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    now = now or clock.now()
    snap = snapshot or {"issues": [], "prs": [], "branches": []}
    install = install or {}
    repo = _get(cfg, "project.repo")
    units = state["units"]
    by_pr = {u["pr"]: k for k, u in units.items() if u.get("pr") is not None}
    titles = {f"issue-{i['number']}": i["title"] for i in snap.get("issues", [])}
    for pr in snap.get("prs", []):
        titles.setdefault(by_pr.get(pr["number"], f"pr-{pr['number']}"), pr["title"])
    events = [e for e in events if not e.get("invalid")]

    running = []
    for key, lease in sorted(leases.active(state).items(), key=lambda kv: kv[1]["granted_at"], reverse=True):
        role = ROLE_OF_KIND.get(lease["kind"], lease["kind"])
        role = MODEL_ROLE.get(role, role)
        running.append({"unit": key, "kind": lease["kind"], "title": _title(key, titles),
                        "model": _get(cfg, f"models.{role}.model") or None,
                        "effort": _get(cfg, f"models.{role}.effort") or None, "session": lease["holder_session"],
                        "started_at": lease["granted_at"], "expires_at": lease["expires_at"],
                        "attempt": lease["attempt"], "pr": units[key].get("pr")})  # fmt: skip

    needs = []
    for qid, q in sorted(state["questions"].items(), key=lambda kv: kv[1]["due_at"]):
        unit = units.get(q["unit"] or "")
        label = f"PR #{unit['pr']}" if unit and unit.get("pr") is not None else (q["unit"] or "")
        cap = int(_get(cfg, f"review.rounds.{(unit or {}).get('tier') or 'normal'}")) + int((unit or {}).get(
            "extra_rounds", 0))
        needs.append({"question_id": qid, "kind": q["kind"], "unit": q["unit"],
                      "title": QUESTION_TITLES.get(q["kind"], "A decision is waiting").format(unit=label, cap=cap),
                      "detail": q.get("detail") or (unit or {}).get("violation") or "",
                      "options": q["options"], "default_option": q["default_option"], "due_at": q["due_at"],
                      "url": _url(repo, unit) if unit else None})  # fmt: skip

    cap = int(_get(cfg, "capacity.max_sessions"))
    busy = len(running)
    if plan is None:
        lines = stop.parse(stop_text)
        scope = {}
        if snapshot is not None:
            from loopctl import snapshot as snapshot_mod

            scope = snapshot_mod.scope(snapshot, state)
        epoch = (state.get("controller") or {}).get("epoch") or 1
        plan = admission.plan(state, cfg, epoch=epoch, snapshot=scope, stopped=lambda k, u: stop.matching(lines, k, u),
                              now=now)  # fmt: skip
    queue = [{"unit": c.unit, "kind": c.kind, "priority": units[c.unit].get("priority"),
              "title": _title(c.unit, titles), "why": c.why} for c in candidates(state, cfg)]  # fmt: skip

    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = budget.week_start(cfg, now)
    window_spend = [s for s in state.get("spend", []) if clock.parse(s["at"]) >= now - budget.WINDOW]
    oldest = min((clock.parse(s["at"]) for s in window_spend), default=now)
    spend = {
        "window": {"usd": budget.spent_since(state, now - budget.WINDOW), "cap_usd": round(budget.window_cap(cfg, now), 4),
                   "resets_at": clock.stamp_at(oldest + budget.WINDOW)},
        "week": {"usd": budget.spent_since(state, week), "cap_usd": round(budget.week_cap(cfg), 4),
                 "resets_at": clock.stamp_at(week + timedelta(days=7))},
        "by_kind": {k: {"window_usd": budget.spent_since(state, now - budget.WINDOW, k),
                        "week_usd": budget.spent_since(state, week, k)} for k in _get(cfg, "work.kinds")},
    }  # fmt: skip

    history = _history(events)
    horizon = now - timedelta(days=UNIT_DAYS)
    shown = {}
    for key, u in sorted(units.items()):
        recent = [h for h in history.get(key, []) if clock.parse(h["at"]) >= horizon]
        if u["state"] in ("merged", "closed") and not recent:
            continue
        doc = dict(u, title=_title(key, titles), history=history.get(key, [])[-HISTORY_MAX:])
        if (url := _url(repo, u)) is not None:
            doc["url"] = url
        shown[key] = doc

    prs = []
    pr_of_branch = {}
    for pr in sorted(snap.get("prs", []), key=lambda p: p["number"], reverse=True):
        key = by_pr.get(pr["number"]) or (f"pr-{pr['number']}" if f"pr-{pr['number']}" in units else None)
        st = "merged" if pr.get("merged") else ("closed" if pr["state"] == "closed" else
                                                ("draft" if pr.get("draft") else "open"))  # fmt: skip
        prs.append({"number": pr["number"], "title": pr["title"], "state": st, "unit": key,
                    "unit_state": units[key]["state"] if key else None, "branch": pr["branch"],
                    "url": f"https://github.com/{repo}/pull/{pr['number']}"})  # fmt: skip
        pr_of_branch.setdefault(pr["branch"], (pr["number"], st))
    branches = [{"name": b, "state": pr_of_branch[b][1] if b in pr_of_branch else "no PR",
                 "pr": pr_of_branch[b][0] if b in pr_of_branch else None}
                for b in snap.get("branches", []) if b != _get(cfg, "project.main")]  # fmt: skip

    lease = state.get("controller")
    next_tick = None
    if lease and lease.get("tick_task"):
        next_tick = clock.stamp_at(now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return {
        "version": VERSION,
        "generated_at": clock.stamp_at(now),
        "tick_id": tick_id,
        "last_tick_at": state.get("last_tick_at"),
        "next_tick_at": next_tick,
        "sweep_minutes": SWEEP_MINUTES,
        "repos": {"target": repo, "state": _get(cfg, "project.state"), "state_branch": "main",
                  "control_issue": install.get("control_issue")},
        "config": cfg.as_dict(),
        "config_hash": cfg.hash,
        "config_sources": {k: s.source for k, s in sorted(cfg.settings.items())},
        "stop": _stop_lines(stop_text, stop_authors),
        "main": {"sha": state["main"]["sha"], "green": state["main"]["green"]},
        "running": running,
        "needs_owner": needs,
        "slots": {"max": cap, "busy": busy, "idle": _idle(plan, cap, busy)},
        "queue": queue,
        "throughput": {"today": _throughput(events, state, day), "week": _throughput(events, state, week)},
        "spend": spend,
        "recent": _recent(events),
        "units": shown,
        "prs": prs,
        "branches": branches,
    }


def text(doc: Mapping[str, Any]) -> str:
    """The same document for a terminal."""
    out = [f"agent-loop status  {doc['generated_at']}  (last tick {doc['last_tick_at'] or 'never'}, "
           f"next {doc['next_tick_at'] or 'not scheduled'})"]  # fmt: skip
    if doc["stop"]["lines"]:
        out.append("STOP: " + "; ".join(line["line"] for line in doc["stop"]["lines"]))
    out.append(f"\n(2) Waiting for you: {len(doc['needs_owner']) or 'nothing'}")
    for q in doc["needs_owner"]:
        opts = ", ".join(f"{o['id']} ({o['label']})" for o in q["options"])
        out.append(f"  - {q['title']} [{q['question_id']}] {q['detail']} options: {opts}")
    out.append(f"\n(1) Running ({doc['slots']['busy']} of {doc['slots']['max']}):")
    out += [f"  - {r['kind']} {r['unit']} {r['title']!r} model {r['model'] or 'default'} since {r['started_at']}"
            for r in doc["running"]] or ["  - nothing"]  # fmt: skip
    out.append("\n(3) Idle slots:")
    out += [f"  - {i['kind'] or 'any'}: {i['why']}" + (f" -> {i['change']}" if i["change"] else "")
            for i in doc["slots"]["idle"]] or ["  - none"]  # fmt: skip
    out.append("\n(4) Queue:")
    out += [f"  {n}. {q['kind']} {q['unit']} {q['title']!r} ({q['why']})" for n, q in enumerate(doc["queue"], 1)] or [
        "  (empty)"]
    t = doc["throughput"]
    out.append(f"\n(5) Throughput: today {t['today']['merged']} merged, {t['today']['units_done']} done; "
               f"week {t['week']['merged']} merged, {t['week']['units_done']} done")  # fmt: skip
    s = doc["spend"]
    out.append(f"(6) Spend: window ${s['window']['usd']:.2f} of ${s['window']['cap_usd']:.2f}, "
               f"week ${s['week']['usd']:.2f} of ${s['week']['cap_usd']:.2f}")  # fmt: skip
    out.append("\n(7) Recent:")
    out += [f"  - {r['at']} {r['type']}: {r['text']}" for r in doc["recent"]] or ["  - nothing"]
    out.append(f"\n(8) Units: {len(doc['units'])}  (9) PRs: {len(doc['prs'])}, branches: {len(doc['branches'])}")
    out.append(f"(10) Next tick: {doc['next_tick_at'] or 'not scheduled'}")
    return "\n".join(out) + "\n"


def unit_text(doc: Mapping[str, Any], key: str) -> str:
    u = doc["units"].get(key)
    if u is None:
        return f"no unit {key} in the status\n"
    lines = [f"{key}: {u['title']!r} is {u['state']} (attempt {u['attempt']}, round {u['round']})"]
    lines += [f"  {h['at']} {h['text']}" for h in u["history"]]
    return "\n".join(lines) + "\n"
