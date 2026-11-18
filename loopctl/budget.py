"""The budget in list-price dollars, per rolling 5-hour window and per plan week (phase2-design §7, decision record
0005; invariant I2).

- **Spend** comes from the sessions' own reported cost (`cost.observed`, cumulative per session): the reducer records
  each increase as a delta stamped by the clock (`state.spend`, 8 days kept) and the session's latest total.
- **Caps**: week = `budget.week` × 100 × `budget.calibration.usd_per_pct` (the plan's week in dollars, $11.5 per 1%
  by default); window = `budget.calibration.window_usd` × the window share now (`budget.window`: a share or a schedule
  `[{days, from, to, tz, share}, …, {share}]`, first matching entry wins, the last is the default).
- **Room** = cap − spent − projected remaining cost of running sessions. A unit is admitted only if its projected
  cost fits both rooms. Projected cost = p75 of observed final costs for its kind and tier (seeded from Phase 1:
  fix $3.0, review $3.2, gate $0.6, triage $1.0).
- **Signals only tighten**: `five_hour` warning → window room × 0.75 until reset; `rejected` → no admission until
  reset; `seven_day` rejected → no new fix, triage, scan or build (review and gate keep running so work lands).
- Meter calibration (§7.3) is cut (build-plan §6): the defaults stay; `usd_per_pct` and `window_usd` are settable.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from loopctl import clock
from loopctl.risk import _get

SEED_USD = {"fix": 3.0, "review": 3.2, "gate": 0.6, "triage": 1.0, "scan": 3.0, "build": 3.0, "reconcile": 1.0,
            "check": 0.6, "revert": 1.0}  # check: a gate run on main; revert: one scripted commit and a PR
WINDOW = timedelta(hours=5)
KEEP = timedelta(days=8)
SAMPLES = 50
WARNING_FACTOR = 0.75
NEW_WORK = ("fix", "triage", "scan", "build", "reconcile")  # stopped by a seven_day rejection
DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def window_share(cfg: Any, now: datetime) -> float:
    """The share of the 5-hour window that applies at `now` (schedules evaluated in each entry's time zone)."""
    value = _get(cfg, "budget.window")
    if not isinstance(value, list):
        return float(value)
    for entry in value[:-1]:
        local = now.astimezone(ZoneInfo(entry["tz"]))
        day = next(d for d, i in DAY_INDEX.items() if i == local.weekday())
        hhmm = local.strftime("%H:%M")
        start, end = entry["from"], entry["to"]
        inside = start <= hhmm < end if start <= end else (hhmm >= start or hhmm < end)
        if day in entry["days"] and inside:
            return float(entry["share"])
    return float(value[-1]["share"])


def week_start(cfg: Any, now: datetime) -> datetime:
    """The start of the plan week containing `now` (`budget.week_start`, e.g. "mon 00:00 Europe/Stockholm")."""
    day, hhmm, tz = _get(cfg, "budget.week_start").split()
    local = now.astimezone(ZoneInfo(tz))
    hour, minute = map(int, hhmm.split(":"))
    start = (local - timedelta(days=(local.weekday() - DAY_INDEX[day]) % 7)).replace(hour=hour, minute=minute,
                                                                                    second=0, microsecond=0)  # fmt: skip
    if start > local:
        start -= timedelta(days=7)
    return start


def week_cap(cfg: Any) -> float:
    return float(_get(cfg, "budget.week")) * 100 * float(_get(cfg, "budget.calibration.usd_per_pct"))


def window_cap(cfg: Any, now: datetime) -> float:
    return window_share(cfg, now) * float(_get(cfg, "budget.calibration.window_usd"))


def p75(values: list[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values")
    k = (len(ordered) - 1) * 0.75
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def projected(state: Mapping[str, Any], kind: str, tier: str | None = None) -> float:
    """p75 of observed final costs for (kind, tier), then for the kind, then the Phase 1 seed."""
    samples = state.get("cost_samples", {})
    for key in (f"{kind}:{tier or 'normal'}", kind):
        if len(samples.get(key, [])) >= 3:
            return round(p75(samples[key]), 4)
    return SEED_USD[kind]


def running_projection(state: Mapping[str, Any]) -> float:
    """Projected remaining cost of every running lease: its projection minus what its session has spent so far."""
    total = 0.0
    sessions = state.get("sessions", {})
    for unit in state["units"].values():
        lease = unit.get("lease")
        if not lease:
            continue
        spent = sessions.get(lease["holder_session"], {}).get("usd", 0.0) if lease["holder_session"] else 0.0
        total += max(0.0, projected(state, lease["kind"], unit.get("tier")) - spent)
    return round(total, 4)


def spent_since(state: Mapping[str, Any], since: datetime, kind: str | None = None) -> float:
    return round(sum(s["usd"] for s in state.get("spend", []) if clock.parse(s["at"]) >= since
                     and (kind is None or s["kind"] == kind)), 4)  # fmt: skip


@dataclass(frozen=True)
class Rooms:
    window_cap: float
    week_cap: float
    window_spent: float
    week_spent: float
    running: float
    window: float  # room left in the window after spend and running projections (and signals)
    week: float
    blocked: tuple[str, ...]  # kinds no signal lets start
    reasons: tuple[str, ...]

    def fits(self, cost: float) -> bool:
        return cost <= self.window + 1e-9 and cost <= self.week + 1e-9


def rooms(state: Mapping[str, Any], cfg: Any, now: datetime | None = None) -> Rooms:
    now = now or clock.now()
    wcap, kcap = window_cap(cfg, now), week_cap(cfg)
    wspent = spent_since(state, now - WINDOW)
    kspent = spent_since(state, week_start(cfg, now))
    running = running_projection(state)
    window, week = wcap - wspent - running, kcap - kspent - running
    blocked: list[str] = []
    reasons: list[str] = []
    signals = state.get("signals", {})
    five, seven = signals.get("five_hour"), signals.get("seven_day")
    if five and clock.parse(five["until_at"]) > now:
        if five["status"] == "rejected":
            window = min(window, 0.0)
            blocked += ["fix", "review", "gate", "triage", "scan", "build", "reconcile", "check", "revert"]
            reasons.append(f"five-hour limit reached until {five['until_at']}")
        else:
            window = (wcap * WARNING_FACTOR) - wspent - running
            reasons.append(f"five-hour limit warning: window room cut by 25% until {five['until_at']}")
    if seven and clock.parse(seven["until_at"]) > now and seven["status"] == "rejected":
        blocked += [k for k in NEW_WORK if k not in blocked]
        reasons.append(f"weekly limit reached until {seven['until_at']}: only review and gate run")
    return Rooms(round(wcap, 4), round(kcap, 4), wspent, kspent, running, round(window, 4), round(week, 4),
                 tuple(dict.fromkeys(blocked)), tuple(reasons))  # fmt: skip


# ----------------------------------------------------------------------------------------- reducer integration
def on_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    """Budget facts from events (called by the reducer for cost.observed, lease.bound and limit.observed)."""
    t, p = event["type"], event["payload"]
    state.setdefault("spend", [])
    state.setdefault("sessions", {})
    state.setdefault("cost_samples", {})
    state.setdefault("signals", {})
    if t == "lease.bound":
        unit = state["units"][event["unit"]]
        state["sessions"].setdefault(p["session"], {"unit": unit["key"], "kind": unit["lease"]["kind"],
                                                    "tier": unit.get("tier"), "usd": 0.0, "final": False})  # fmt: skip
    elif t == "cost.observed":
        sess = state["sessions"].setdefault(p["session"], {"unit": event["unit"], "kind": None, "tier": None,
                                                           "usd": 0.0, "final": False})  # fmt: skip
        if sess["final"]:
            return
        delta = round(p["usd"] - sess["usd"], 6)
        if delta > 0:  # cumulative: only increases count (a lower reading is ignored, never refunded)
            state["spend"].append({"at": event["at"], "usd": delta, "kind": sess["kind"], "unit": sess["unit"],
                                   "session": p["session"]})  # fmt: skip
            sess["usd"] = round(p["usd"], 6)
        if p["final"]:
            sess["final"] = True
            if sess["kind"]:
                for key in (sess["kind"], f"{sess['kind']}:{sess['tier'] or 'normal'}"):
                    state["cost_samples"].setdefault(key, []).append(sess["usd"])
                    state["cost_samples"][key] = state["cost_samples"][key][-SAMPLES:]
        horizon = clock.parse(event["at"]) - KEEP
        state["spend"] = [s for s in state["spend"] if clock.parse(s["at"]) >= horizon]
    elif t == "limit.observed":
        if p["status"] == "allowed":
            return  # signals only tighten: an "allowed" reading never lifts an earlier one before its reset
        until = clock.stamp_at(clock.parse(event["at"]) + timedelta(seconds=p["resets_in_s"]))
        old = state["signals"].get(p["window"])
        if old and old["status"] == "rejected" and p["status"] == "allowed_warning" and old["until_at"] > event["at"]:
            return  # a warning doesn't soften a rejection in force
        state["signals"][p["window"]] = {"status": p["status"], "until_at": until}
