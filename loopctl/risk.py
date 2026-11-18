"""Risk tiers from changed paths, critical changes, gate files and scope exclusions (phase2-design §9.1, §9.4 (6),
§6.2 `[risk]` and `[scope]`; decision record 0009; invariant I9).

Ported from v1's `scripts/loop_rules.py` (with its tests' cases): the data-loss SQL detection and the glob reading.
`**` spans any number of directories (none included), `*` and `?` stay inside one path part, and a pattern without a
`/` matches the file name in any directory (`*.md` matches `docs/a/b.md`).

| Tier | When | Cap |
|---|---|---|
| docs | every changed file matches `project.docs_only` | `review.rounds.docs` (1) |
| normal | anything else | `review.rounds.normal` (2) |
| high | any file matches `risk.high`, a migration, or raised by the owner | `review.rounds.high` (4) |
| critical (overlay) | `risk.critical`, or a migration that deletes data (`risk.critical_sql`) | the tier's cap + owner approval |
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

# Statements that delete data. `ON DELETE CASCADE`, `DROP INDEX`, `DROP NOT NULL`, `DROP CONSTRAINT` and the like
# change no rows; `ALTER TABLE t DROP c` (COLUMN is optional) does. (From v1 loop_rules.DATA_LOSS.)
DATA_LOSS = re.compile(
    r"\bTRUNCATE\b|\bDELETE\s+FROM\b|\bDROP\s+(?!(?:INDEX|CONSTRAINT|DEFAULT|NOT\s+NULL|TRIGGER|FUNCTION|PROCEDURE"
    r"|VIEW|POLICY|IDENTITY|EXPRESSION|STATISTICS|RULE)\b)",
    re.IGNORECASE,
)
MIGRATION = re.compile(r"(^|/)(migrations?|alembic/versions)/", re.IGNORECASE)
ALWAYS_GATE = (".github/**",)  # precondition 6 names `.github/` besides `risk.gate_files`
TIERS = ("docs", "normal", "high")


def without_line_comment(line: str) -> str:
    """The line up to a `--` comment that isn't inside a '…' string."""
    quoted = False
    for i, char in enumerate(line):
        if char == "'":
            quoted = not quoted
        elif not quoted and line.startswith("--", i):
            return line[:i]
    return line


def added_sql(patch: str) -> str:
    """The SQL a patch adds, without comments."""
    lines = [line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++")]
    return re.sub(r"/\*.*?\*/", " ", "\n".join(without_line_comment(line) for line in lines), flags=re.DOTALL)


def data_loss(patch: str) -> str | None:
    """The first data-deleting statement a patch adds (upper case, one space between words), else None."""
    statement = DATA_LOSS.search(added_sql(patch))
    return " ".join(statement.group(0).split()).upper() if statement else None


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    if "/" not in pattern.rstrip("/"):
        pattern = "**/" + pattern
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]*/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif pattern[i] == "*":
            out.append(r"[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(path: str, pattern: str) -> bool:
    return _glob_regex(pattern).match(path) is not None


def any_match(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_match(path, g) for g in patterns)


def all_match(paths: Sequence[str], patterns: Sequence[str]) -> bool:
    """Every path matches one of the patterns (and there is at least one path)."""
    return bool(paths) and all(any_match(p, patterns) for p in paths)


def _get(cfg: Any, key: str) -> Any:
    """A setting from a loopctl.config.Config or a plain {dotted key: value} mapping."""
    return cfg[key] if isinstance(cfg, Mapping) else cfg.value(key)


@dataclass(frozen=True)
class Risk:
    tier: str
    critical: bool
    gate_files: bool  # precondition 6: touches risk.gate_files or .github/
    reasons: tuple[str, ...]


def assess(paths: Sequence[str], cfg: Any, patches: Mapping[str, str | None] | None = None, raised: bool = False) -> Risk:
    """The PR's tier and overlays from its changed `paths` (and, for SQL, their `patches`: path -> unified diff)."""
    paths = list(paths)
    patches = patches or {}
    reasons: list[str] = []
    high = [p for p in paths if any_match(p, _get(cfg, "risk.high"))]
    migrations = [p for p in paths if MIGRATION.search(p)]
    if raised:
        tier = "high"
        reasons.append("raised by the owner")
    elif high or migrations:
        tier = "high"
        reasons += [f"high-risk path {p}" for p in high[:3]] + [f"migration {p}" for p in migrations[:3]]
    elif all_match(paths, _get(cfg, "project.docs_only")):
        tier = "docs"
        reasons.append("docs only")
    else:
        tier = "normal"
    critical_paths = [p for p in paths if any_match(p, _get(cfg, "risk.critical"))]
    reasons += [f"critical path {p}" for p in critical_paths[:3]]
    sql = None
    if _get(cfg, "risk.critical_sql"):
        for p in migrations:
            if p not in patches or patches[p] is None:  # GitHub omits large diffs: fail closed (as v1 did)
                sql = "no diff"
                reasons.append(f"migration {p} has no diff to check")
                break
            sql = data_loss(patches[p])
            if sql:
                reasons.append(f"migration {p} deletes data ({sql})")
                break
    gate = [p for p in paths if any_match(p, list(_get(cfg, "risk.gate_files")) + list(ALWAYS_GATE))]
    reasons += [f"gate file {p}" for p in gate[:3]]
    return Risk(tier, bool(critical_paths or sql), bool(gate), tuple(reasons))


def cap(tier: str, cfg: Any) -> int:
    """The review round cap of a tier (§9.1)."""
    return int(_get(cfg, f"review.rounds.{tier}"))


def excluded(labels: Iterable[str], paths: Sequence[str], cfg: Any) -> str | None:
    """Why a unit may never be leased (I9): an excluded label, or only excluded paths. None if it may be."""
    bad = sorted(set(labels) & set(_get(cfg, "scope.excluded_labels")))
    if bad:
        return f"label {bad[0]} is in scope.excluded_labels"
    if all_match(list(paths), _get(cfg, "scope.excluded_paths")):
        return "every changed path is in scope.excluded_paths"
    return None
