"""Low-priority findings become notes in `notes.json`, not GitHub issues (phase2-design §9.5, decision record 0012).

`notes.json` (written by the controller): `{"version": 1, "notes": {id: note}}`, a note being `{path, summary, area,
source_pr, source_round, first_seen, last_seen, seen, recheck}` with times from loopctl's clock. Deduplicated on
(path, normalised summary). Pruned: `notes.ttl_days` after last seen; when a merged PR changed the noted file and the
next review of that file doesn't report it again; at most 20 per area and 200 in all (oldest last-seen out).
All functions are pure: they take a document and return a new one.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Any

from loopctl import clock

PER_AREA = 20
TOTAL = 200
BRIEF_MAX = 5


def empty() -> dict[str, Any]:
    return {"version": 1, "notes": {}}


def normalise(summary: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", summary.lower()).strip()


def note_id(path: str, summary: str) -> str:
    return "n-" + hashlib.sha256(f"{path}\n{normalise(summary)}".encode()).hexdigest()[:16]


def area(path: str) -> str:
    """The directory a note belongs to, at most two levels deep (`knopp/core/safety/pii.py` -> `knopp/core`)."""
    parts = path.split("/")[:-1]
    return "/".join(parts[:2]) or "."


def add(doc: Mapping[str, Any], findings: Iterable[Mapping[str, Any]], *, pr: int, round_: int,
        ttl_days: int = 30) -> dict[str, Any]:
    """Record note-routed findings ({path, summary}); a repeat bumps `seen` and `last_seen` and clears `recheck`."""
    out = copy.deepcopy(dict(doc)) if doc else empty()
    now = clock.stamp()
    for f in findings:
        nid = note_id(f["path"], f["summary"])
        note = out["notes"].get(nid)
        if note:
            note.update(last_seen=now, seen=note["seen"] + 1, recheck=False)
        else:
            out["notes"][nid] = {"path": f["path"], "summary": f["summary"][:300], "area": area(f["path"]),
                                 "source_pr": pr, "source_round": round_, "first_seen": now, "last_seen": now,
                                 "seen": 1, "recheck": False}  # fmt: skip
    return prune(out, ttl_days)


def mark_changed(doc: Mapping[str, Any], paths: Iterable[str]) -> dict[str, Any]:
    """A merged PR changed these files: their notes are dropped unless the next review reports them again."""
    out = copy.deepcopy(dict(doc))
    changed = set(paths)
    for note in out["notes"].values():
        if note["path"] in changed:
            note["recheck"] = True
    return out


def after_review(doc: Mapping[str, Any], reviewed_paths: Iterable[str], reported: Iterable[str]) -> dict[str, Any]:
    """After a review whose PR changed `reviewed_paths`: notes marked for recheck on those files and not among the
    `reported` note ids are pruned."""
    out = copy.deepcopy(dict(doc))
    paths, keep = set(reviewed_paths), set(reported)
    out["notes"] = {k: n for k, n in out["notes"].items() if not (n["recheck"] and n["path"] in paths and k not in keep)}
    return out


def prune(doc: Mapping[str, Any], ttl_days: int = 30) -> dict[str, Any]:
    out = copy.deepcopy(dict(doc))
    horizon = clock.now() - timedelta(days=ttl_days)
    notes = {k: n for k, n in out["notes"].items() if clock.parse(n["last_seen"]) > horizon}
    newest_first = sorted(notes.items(), key=lambda kv: (kv[1]["last_seen"], kv[0]), reverse=True)
    kept: dict[str, Any] = {}
    per_area: dict[str, int] = {}
    for k, n in newest_first:
        if len(kept) >= TOTAL or per_area.get(n["area"], 0) >= PER_AREA:
            continue
        per_area[n["area"]] = per_area.get(n["area"], 0) + 1
        kept[k] = n
    out["notes"] = dict(sorted(kept.items()))
    return out


def for_brief(doc: Mapping[str, Any], touched: Iterable[str]) -> list[dict[str, Any]]:
    """Up to five notes on the files a fix touches, newest first ("fix only if it is inside the task and small")."""
    paths = set(touched)
    hits = [dict(n, id=k) for k, n in doc.get("notes", {}).items() if n["path"] in paths]
    return sorted(hits, key=lambda n: n["last_seen"], reverse=True)[:BRIEF_MAX]
