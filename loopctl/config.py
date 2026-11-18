"""One config schema, presets, precedence and provenance (phase2-design §6, decision record 0006, proof test T-CFG).

Every key is declared once in `SCHEMA` with a type, a default and where it may be set:

- `project`: only in the target's `.agent-loop.toml` (changed by PR);
- `runtime`: only by owner commands;
- `both`: either (the file gives a default, a command overrides it).

Precedence, lowest first: engine default < preset (chosen by `run.mode`) < project file < runtime command. Unknown
keys are an error naming the key, the file and the line. There is no free-text rule field.

    cfg = resolve(project_text=Path(".agent-loop.toml").read_text(), project_file=".agent-loop.toml",
                  commands=[Override("budget.week", 0.4, "command c-0192 by owner at 2026-11-18T08:12:00.000Z")])
    cfg.value("budget.week"); cfg.explain(); cfg.as_dict(); cfg.hash
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from loopctl.canonical import config_hash

KINDS = ("fix", "review", "gate", "triage", "scan", "build")
MODES = ("off", "shadow", "steady", "push")
ROLES = ("controller", "triage", "fix", "build", "review", "reviewer", "verifier", "gate")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DURATION = re.compile(r"^\d+(m|h|d)$")
TZ = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+)*$")


class ConfigError(ValueError):
    """The config can't be used as it is. The message names the key, and the file and line when there is one."""


# ---------------------------------------------------------------------------------------------------------- types
# A checker takes (key, value) and returns the normalised value or raises ConfigError.
Checker = Callable[[str, Any], Any]


def _fail(key: str, want: str, value: Any) -> ConfigError:
    return ConfigError(f"{key} must be {want}, got {value!r}")


def t_str(key: str, v: Any) -> str:
    if not isinstance(v, str):
        raise _fail(key, "a string", v)
    return v


def t_bool(key: str, v: Any) -> bool:
    if not isinstance(v, bool):
        raise _fail(key, "true or false", v)
    return v


def t_int(lo: int, hi: int) -> Checker:
    def check(key: str, v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise _fail(key, f"an integer from {lo} to {hi}", v)
        return v

    return check


def t_share(key: str, v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise _fail(key, "a share from 0 to 1", v)
    return round(float(v), 4)


def t_usd(key: str, v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1_000_000:
        raise _fail(key, "an amount in USD from 0 to 1000000", v)
    return round(float(v), 4)


def t_enum(*choices: str) -> Checker:
    def check(key: str, v: Any) -> str:
        if v not in choices:
            raise _fail(key, "one of " + ", ".join(repr(c) for c in choices), v)
        return v

    return check


def t_list(item: Checker, *, unique: bool = True) -> Checker:
    def check(key: str, v: Any) -> list[Any]:
        if not isinstance(v, (list, tuple)):
            raise _fail(key, "a list", v)
        out = [item(f"{key}[{i}]", x) for i, x in enumerate(v)]
        if unique and len(set(map(repr, out))) != len(out):
            raise ConfigError(f"{key} lists a value twice: {v!r}")
        return out

    return check


def t_glob(key: str, v: Any) -> str:
    v = t_str(key, v)
    if not v or v.startswith("/") or ".." in v.split("/"):
        raise _fail(key, "a relative path pattern", v)
    return v


def t_repo(key: str, v: Any) -> str:
    v = t_str(key, v)
    if v and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", v):
        raise _fail(key, 'an "owner/name" repository', v)
    return v


def t_issue(key: str, v: Any) -> int:
    return t_int(1, 10_000_000)(key, v)


def t_tz(key: str, v: Any) -> str:
    if not isinstance(v, str) or not TZ.fullmatch(v):
        raise _fail(key, 'an IANA time zone such as "Europe/Stockholm"', v)
    return v


def t_hours(key: str, v: Any) -> dict[str, str]:
    """`{from = "22:00", to = "07:00", tz = "Europe/Stockholm"}`, or an empty table for none."""
    if not isinstance(v, Mapping):
        raise _fail(key, "a table {from, to, tz}", v)
    if not v:
        return {}
    extra = set(v) - {"from", "to", "tz"}
    if extra:
        raise ConfigError(f"{key} has unknown field(s) {sorted(extra)}; allowed: from, to, tz")
    for part in ("from", "to"):
        if not isinstance(v.get(part), str) or not HHMM.fullmatch(v[part]):
            raise _fail(f"{key}.{part}", 'a time "HH:MM"', v.get(part))
    return {"from": v["from"], "to": v["to"], "tz": t_tz(f"{key}.tz", v.get("tz"))}


def t_window(key: str, v: Any) -> float | list[dict[str, Any]]:
    """A share, or a schedule: entries `{days, from, to, tz, share}` and a last entry `{share}` as the default."""
    if not isinstance(v, (list, tuple)):
        return t_share(key, v)
    if not v:
        raise ConfigError(f"{key} schedule is empty; give a share or at least a default entry {{share = …}}")
    out: list[dict[str, Any]] = []
    for i, entry in enumerate(v):
        k = f"{key}[{i}]"
        if not isinstance(entry, Mapping):
            raise _fail(k, "a table", entry)
        last = i == len(v) - 1
        allowed = {"share"} if last else {"days", "from", "to", "tz", "share"}
        if set(entry) - allowed or "share" not in entry or (not last and set(entry) != allowed):
            want = "only {share} (the default)" if last else "exactly {days, from, to, tz, share}"
            raise ConfigError(f"{k} must have {want}, got {sorted(entry)}")
        item: dict[str, Any] = {"share": t_share(f"{k}.share", entry["share"])}
        if not last:
            days = t_list(t_enum(*DAYS))(f"{k}.days", entry["days"])
            if not days:
                raise ConfigError(f"{k}.days is empty")
            item.update(days=days, tz=t_tz(f"{k}.tz", entry["tz"]))
            for part in ("from", "to"):
                if not isinstance(entry[part], str) or not HHMM.fullmatch(entry[part]):
                    raise _fail(f"{k}.{part}", 'a time "HH:MM"', entry[part])
                item[part] = entry[part]
        out.append(item)
    return out


def t_duration(key: str, v: Any) -> str:
    if not isinstance(v, str) or not DURATION.fullmatch(v):
        raise _fail(key, 'a duration such as "45m", "1h" or "2d"', v)
    return v


def t_week_start(key: str, v: Any) -> str:
    """`"<day> HH:MM <tz>"`, e.g. "mon 00:00 Europe/Stockholm"."""
    parts = v.split() if isinstance(v, str) else []
    if len(parts) != 3 or parts[0] not in DAYS or not HHMM.fullmatch(parts[1]) or not TZ.fullmatch(parts[2]):
        raise _fail(key, '"<day> HH:MM <time zone>", e.g. "mon 00:00 Europe/Stockholm"', v)
    return v


# --------------------------------------------------------------------------------------------------------- schema
@dataclass(frozen=True)
class Key:
    name: str
    check: Checker
    default: Any
    where: str  # "project" | "runtime" | "both"
    doc: str


STEADY_WINDOW = [
    {"days": ["mon", "tue", "wed", "thu", "fri"], "from": "08:00", "to": "18:00", "tz": "Europe/Stockholm",
     "share": 0.3},
    {"share": 0.8},
]  # fmt: skip


def _keys() -> list[Key]:
    p, r, b = "project", "runtime", "both"
    keys = [
        Key("project.repo", t_repo, "", p, "the target repository, owner/name"),
        Key("project.main", t_str, "main", p, "the target's main branch"),
        Key("project.state", t_repo, "", p, "the private state repository, owner/name"),
        Key("project.gate", t_str, "", p, "the gate command run on every head before merge"),
        Key("project.gate_docs", t_str, "", p, "the gate command for docs-only changes (empty: the full gate)"),
        Key("project.docs_only", t_list(t_glob), ["docs/**", "*.md"], p, "paths whose changes are docs-only"),
        Key("risk.high", t_list(t_glob), [], p, "paths that make a PR high-risk (4 review rounds)"),
        Key("risk.critical", t_list(t_glob), [], p, "paths that need the owner's approval before merge"),
        Key("risk.critical_sql", t_bool, True, p, "a migration that deletes data is critical"),
        Key("risk.gate_files", t_list(t_glob), [".agent-loop.toml", ".github/**"], p,
            "gate files: a PR touching them needs an owner approval newer than its head"),
        Key("scope.excluded_labels", t_list(t_str), [], b, "issues with these labels are never leased"),
        Key("scope.excluded_paths", t_list(t_glob), [], b, "findings here become notes unless severity ≥ high"),
        Key("scope.freeze", t_bool, False, b, "feature freeze: build off; review rejects new features"),
        Key("scan.targets", t_enum("", "hardening"), "", b, "what scan units look for"),
        Key("run.mode", t_enum(*MODES), "steady", b, "preset: off | shadow | steady | push"),
        Key("run.paused", t_bool, False, r, "pause everything"),
        Key("run.paused_kinds", t_list(t_enum(*KINDS)), [], r, "pause these kinds of work"),
        Key("budget.week", t_share, 0.5, b, "share of the plan's week the loop may spend"),
        Key("budget.window", t_window, STEADY_WINDOW, b, "share of the 5-hour window, or a schedule of shares"),
        Key("budget.week_start", t_week_start, "mon 00:00 Europe/Stockholm", b, "when the plan's week starts"),
        Key("budget.calibration.usd_per_pct", t_usd, 11.5, b, "list-price USD per 1% of the week (fitted)"),
        Key("budget.calibration.window_usd", t_usd, 100.0, b, "list-price USD in one 5-hour window"),
        Key("budget.evals_usd_month", t_usd, 0.0, b, "product evals, a separate meter"),
        Key("schedule.quiet_hours", t_hours, {"from": "22:00", "to": "07:00", "tz": "Europe/Stockholm"}, b,
            "quiet hours"),
        Key("schedule.night", t_enum("stop", "drain", "run"), "drain", b,
            "inside quiet hours: stop starting, drain (finish what runs), or run"),
        Key("capacity.max_sessions", t_int(0, 20), 3, b, "hard ceiling on concurrent worker sessions"),
        Key("work.kinds", t_list(t_enum(*KINDS)), ["fix", "review", "gate", "triage"], b, "kinds of work started"),
        Key("work.scope", t_enum("all", "listed"), "all", b, "all accepted issues, or only work.order"),
        Key("work.order", t_list(t_issue), [], b, "issues to take first, in this order"),
        Key("work.min_priority", t_enum("P0", "P1", "P2", "P3"), "P3", b, "lowest priority taken"),
        Key("review.rounds.docs", t_int(1, 10), 1, b, "review round cap for docs-only PRs"),
        Key("review.rounds.normal", t_int(1, 10), 2, b, "review round cap for normal PRs"),
        Key("review.rounds.high", t_int(1, 10), 4, b, "review round cap for high-risk PRs"),
        Key("review.blocking_min_severity", t_enum("critical", "high", "medium", "low"), "medium", b,
            "lowest severity of an introduced finding that blocks the merge"),
        Key("notes.ttl_days", t_int(1, 365), 30, b, "days a low-priority note lives after it was last seen"),
        Key("merge.auto", t_bool, True, b, "merge approved, gated PRs without the owner"),
        Key("merge.batch_max", t_int(1, 20), 6, b, "PRs verified and merged per workflow run"),
        Key("notify.phone", t_list(t_enum("decision", "main_red")), ["decision", "main_red"], b,
            "what reaches the owner's phone"),
        Key("alerts.stage_wait_max", t_duration, "1h", b, "alert when a unit waits longer than this at one stage"),
    ]
    default_models = {"controller": "claude-sonnet-5-5"}
    for role in ROLES:
        keys.append(Key(f"models.{role}.model", t_str, default_models.get(role, ""), b,
                        f"model for {role} sessions (empty: the role default, loopctl/roles.py)"))  # fmt: skip
        keys.append(Key(f"models.{role}.effort", t_enum(*EFFORTS), "", b, f"effort for {role} sessions"))
    return keys


SCHEMA: dict[str, Key] = {k.name: k for k in _keys()}

# Presets set runtime keys only (phase2-design §6.1). Whether a mode starts sessions follows from the mode itself.
PRESETS: dict[str, dict[str, Any]] = {
    "off": {"budget.week": 0.0, "budget.window": 0.0, "schedule.night": "stop", "work.kinds": [],
            "capacity.max_sessions": 0},
    "shadow": {"budget.week": 0.0, "budget.window": 0.0, "work.kinds": list(KINDS), "capacity.max_sessions": 0},
    "steady": {"budget.week": 0.5, "budget.window": STEADY_WINDOW, "schedule.night": "drain",
               "work.kinds": ["fix", "review", "gate", "triage"], "capacity.max_sessions": 3},
    "push": {"budget.week": 0.7, "budget.window": 0.8, "schedule.night": "run", "work.kinds": list(KINDS),
             "capacity.max_sessions": 6},
}  # fmt: skip
STARTS_SESSIONS = {"off": False, "shadow": False, "steady": True, "push": True}


# ----------------------------------------------------------------------------------------------------- resolution
@dataclass(frozen=True)
class Override:
    """A runtime value from an applied owner command. `source` is shown by explain, e.g.
    "command c-0192 by owner at 2026-11-18T08:12:00.000Z" (the time written by loopctl when it applied it)."""

    key: str
    value: Any
    source: str


@dataclass(frozen=True)
class Setting:
    key: str
    value: Any
    source: str
    overrides: tuple[str, ...] = ()  # the sources it replaced, nearest first


@dataclass(frozen=True)
class Config:
    settings: dict[str, Setting] = field(default_factory=dict)

    def value(self, key: str) -> Any:
        if key not in self.settings:
            raise ConfigError(f"unknown key {key}")
        return self.settings[key].value

    @property
    def mode(self) -> str:
        return self.value("run.mode")

    @property
    def starts_sessions(self) -> bool:
        return STARTS_SESSIONS[self.mode]

    def as_dict(self) -> dict[str, Any]:
        """The effective config as nested tables (what `config_hash` is taken over)."""
        out: dict[str, Any] = {}
        for key, setting in self.settings.items():
            node = out
            *parents, leaf = key.split(".")
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = setting.value
        return out

    @property
    def hash(self) -> str:
        return config_hash(self.as_dict())

    def explain(self, key: str | None = None) -> str:
        names = [key] if key else sorted(self.settings)
        lines = []
        for name in names:
            if name not in self.settings:
                raise ConfigError(f"unknown key {name}; `loopctl explain` lists every key")
            s = self.settings[name]
            line = f"{name} = {_show(s.value)}    [{s.source}]"
            if s.overrides:
                line += f" (overrides {', '.join(s.overrides)})"
            lines.append(line)
        return "\n".join(lines)


def _show(value: Any) -> str:
    from loopctl.canonical import canonical

    return canonical(value)


def _flatten(table: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """TOML tables to dotted keys. A table is a leaf when the schema declares that dotted key (budget.window entries,
    schedule.quiet_hours)."""
    out: dict[str, Any] = {}
    for k, v in table.items():
        name = f"{prefix}{k}"
        if isinstance(v, Mapping) and name not in SCHEMA:
            out.update(_flatten(v, name + "."))
        else:
            out[name] = v
    return out


_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-\"' ]+?)\s*\]\s*(#.*)?$")
_ASSIGN = re.compile(r"^\s*([A-Za-z0-9_.\-\"']+)\s*=")


def _lines(text: str) -> dict[str, int]:
    """Best-effort line number of every dotted key assigned in the TOML text (for error messages and explain)."""
    found: dict[str, int] = {}
    table = ""
    for n, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("[["):
            continue
        head = _HEADER.match(raw)
        if head:
            table = head.group(1).replace('"', "").replace("'", "").replace(" ", "")
            continue
        assign = _ASSIGN.match(raw)
        if assign:
            k = assign.group(1).replace('"', "").replace("'", "")
            full = f"{table}.{k}" if table else k
            found.setdefault(full, n)
    return found


def _where(file: str, line: int | None) -> str:
    return f"{file}:{line}" if line else file


def parse_project(text: str, file: str = ".agent-loop.toml") -> dict[str, tuple[Any, str]]:
    """Read the target's `.agent-loop.toml`: {dotted key: (checked value, "file:line")}. Unknown keys, runtime-only
    keys and wrong types raise ConfigError naming the key, the file and the line."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{file}: not valid TOML: {exc}") from exc
    lines = _lines(text)
    out: dict[str, tuple[Any, str]] = {}
    for key, raw in _flatten(data).items():
        line = lines.get(key)
        if key not in SCHEMA:
            hint = _closest(key)
            raise ConfigError(f"{_where(file, line)}: unknown key {key}" + (f" (did you mean {hint}?)" if hint else ""))
        spec = SCHEMA[key]
        if spec.where == "runtime":
            raise ConfigError(f"{_where(file, line)}: {key} can only be set by an owner command, not in {file}")
        try:
            out[key] = (spec.check(key, raw), _where(file, line))
        except ConfigError as exc:
            raise ConfigError(f"{_where(file, line)}: {exc}") from exc
    return out


def check_override(o: Override) -> Override:
    if o.key not in SCHEMA:
        raise ConfigError(f"unknown key {o.key} in {o.source}")
    spec = SCHEMA[o.key]
    if spec.where == "project":
        raise ConfigError(f"{o.key} is a project fact: change it in .agent-loop.toml by PR, not by a command")
    return Override(o.key, spec.check(o.key, o.value), o.source)


def _closest(key: str) -> str | None:
    import difflib

    match = difflib.get_close_matches(key, SCHEMA, n=1, cutoff=0.75)
    return match[0] if match else None


def resolve(project_text: str | None = None, project_file: str = ".agent-loop.toml",
            commands: Sequence[Override] = ()) -> Config:  # fmt: skip
    """Engine default < preset < project file < runtime commands (applied in the given order; later wins)."""
    project = parse_project(project_text, project_file) if project_text is not None else {}
    overrides = [check_override(o) for o in commands]

    layers: dict[str, list[tuple[Any, str]]] = {k: [(spec.default, "engine")] for k, spec in SCHEMA.items()}
    # run.mode decides the preset, so it is resolved first from its own layers.
    mode_layers = layers["run.mode"] + ([project["run.mode"]] if "run.mode" in project else [])
    mode_layers += [(o.value, o.source) for o in overrides if o.key == "run.mode"]
    mode = mode_layers[-1][0]
    for key, value in PRESETS[mode].items():
        layers[key].append((value, f"preset {mode}"))
    for key, value_source in project.items():
        layers[key].append(value_source)
    for o in overrides:
        layers[o.key].append((o.value, o.source))

    settings = {}
    for key, stack in layers.items():
        value, source = stack[-1]
        settings[key] = Setting(key, value, source, tuple(s for _, s in reversed(stack[:-1])))
    return Config(settings)


def keys(where: Iterable[str] = ("project", "runtime", "both")) -> list[str]:
    allowed = set(where)
    return sorted(k for k, spec in SCHEMA.items() if spec.where in allowed)
