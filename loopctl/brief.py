"""`loopctl brief <role> <unit>`: the whole prompt of a worker session, from the store, under a token budget
(phase2-design §8.1–8.2, decision records 0014 and 0029; proof tests T-BRIEF, template lint and T-TIME's second half).

A brief is a role template (`loopctl/templates/roles/<role>.md`) whose `{{placeholders}}` are filled with sections.
Tokens are estimated conservatively as bytes ÷ 3. Sections are filled in priority order: mandatory ones first (if
they don't fit, the brief is not rendered and the unit goes to `needs_owner`: "split or summarise"), then optional
ones while room remains; an optional section that doesn't fit whole is cut at a line with a marker. Some roles carry
material outside the base budget with its own cap (the review's diff, a subagent's slice); over its cap is
`needs_owner` too.

Text that comes from GitHub (issue and PR bodies, finding text) is wrapped as data, so a brief never presents it as
instructions (§12.4). Workers read nothing else from the loop: the templates pass a lint that refuses references to
the store, the engine's docs or skills (tests/test_brief.py). No command in a brief takes a time: every time is
written by code (§11).

The tick (M0.6) builds a `BriefInput` from the store and the GitHub snapshot; until then the CLI reads one from a JSON
file (`--input`), which is also how the tests' fixtures drive it.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TEMPLATES = Path(__file__).resolve().parent / "templates"
ROLES = ("fix", "review", "reviewer", "verifier", "gate", "triage", "reconcile", "check", "revert")
KIND_OF_ROLE = {"fix": "fix", "review": "review", "gate": "gate", "triage": "triage", "check": "check",
                "revert": "revert"}  # roles that hold a lease

# Base budgets in tokens (§8.1). reconcile (G6, build-plan) is a daily worker that runs `loopctl reconcile` (the
# comparison of the store with GitHub, in code) and reports its result: a fixed script, like the gate's.
BUDGET = {"fix": 6000, "review": 4000, "reviewer": 2000, "verifier": 2000, "gate": 500, "triage": 3000,
          "reconcile": 600, "check": 500, "revert": 1500}  # fmt: skip
RECONCILE_FILE = "/tmp/reconcile.json"
ISSUE_BODY_MAX = 3000  # a fix brief's issue body over this is "split or summarise" (§8.2)
TRIAGE_ISSUE_MAX = 1800
DIFF_MAX = 60_000  # a review's diff over this is "split" (§8.2)
SLICE_MAX = 20_000  # a reviewer's or verifier's slice of the diff
NOTES_MAX = 1000  # notes on touched paths in a fix brief (§8.1)
NOTES_COUNT = 5  # at most this many notes (§9.5)
PR_BODY_MAX = 800
CUT = "[… {name} cut here: over the brief's budget]"
PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
DATA_OPEN, DATA_CLOSE = "<<<DATA {label}", "DATA>>>"
FOCUSES = ("correctness", "security", "spec")


def tokens(text: str) -> int:
    """The conservative estimator of §8.2: UTF-8 bytes ÷ 3, rounded up."""
    return -(-len(text.encode("utf-8")) // 3)


class BriefError(ValueError):
    """The input can't make a brief (unknown role, missing field)."""


@dataclass
class Section:
    name: str
    text: str
    mandatory: bool
    priority: int  # lower fills first
    cap: int | None = None  # tokens; over it a mandatory section is needs_owner, an optional one is cut
    extra: bool = False  # outside the base budget (the diff, a slice); then `cap` is its own budget
    why: str = ""  # what to tell the owner when a mandatory section is over

    @property
    def size(self) -> int:
        return tokens(self.text)


@dataclass
class Brief:
    role: str
    unit: str
    text: str = ""
    tokens: int = 0  # the whole brief
    base_tokens: int = 0  # what counts against BUDGET[role]
    budget: int = 0
    cut: list[str] = field(default_factory=list)  # optional sections cut or dropped
    needs_owner: str | None = None  # why the brief can't be rendered (the unit goes to needs_owner)

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role, "unit": self.unit, "tokens": self.tokens, "base_tokens": self.base_tokens,
                "budget": self.budget, "cut": self.cut, "needs_owner": self.needs_owner}  # fmt: skip


@dataclass
class BriefInput:
    """Everything a brief may contain. Built by the tick from the store and the GitHub snapshot (M0.6)."""

    unit: str  # issue-12, pr-40, reconcile-20261118
    token: str = ""  # the lease token (0029): appears only inside the loopctl commands
    controller_session: str = ""  # the controller's session id, for the wake-up message (§13)
    issue: Mapping[str, Any] | None = None  # {number, title, body, labels}
    pr: Mapping[str, Any] | None = None  # {number, title, body, head, branch}
    tier: str | None = None  # docs | normal | high
    critical: bool = False
    round: int = 0
    round_cap: int = 0
    findings: Sequence[Mapping[str, Any]] = ()  # finding.json shape + status (open | fixed | reopened | waived)
    notes: Sequence[Mapping[str, Any]] = ()  # {path, summary, times_seen}
    freeze: bool = False
    diff: str = ""  # review: the PR's diff (round >= 3: the delta since the last reviewed head)
    delta_from: str | None = None  # round >= 3: the last reviewed head
    duplicates: Sequence[Mapping[str, Any]] = ()  # triage: {number, title, state}
    focus: str = "correctness"  # reviewer
    finding: Mapping[str, Any] | None = None  # verifier: the one finding to disprove
    slice: str = ""  # reviewer/verifier: the part of the diff
    revert: Mapping[str, str] | None = None  # revert: {red, good} main shas (§10.6)
    main: str = "main"  # revert: the target's main branch
    attach: str = ""  # owner/name the worker attaches itself (a session starts from one repository: decision 0030)

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> BriefInput:
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        unknown = sorted(set(data) - set(known))
        if unknown:
            raise BriefError(f"unknown brief input field(s): {', '.join(unknown)}")
        if "unit" not in known:
            raise BriefError("the brief input needs a unit")
        return cls(**known)


# --- the loopctl commands a brief gives (the worker commands are built at M0.10 with exactly these forms) -------------


def branch(kind: str, unit: str) -> str:
    """The name of every worker branch the loop creates: `loop/<kind>/<unit>` (snapshot.LOOP_BRANCH)."""
    return f"loop/{kind}/{unit}"


def commands(role: str, src: BriefInput) -> dict[str, str]:
    """The exact command lines for a role. None of them takes a time (T-TIME): code stamps every time."""
    u, t = src.unit, src.token
    kind = KIND_OF_ROLE.get(role, role)
    out = {
        "identify": f"loopctl guard identify {kind} {u} --token {t}",
        "finish": f"loopctl finish {u} --token {t} --outcome <done|blocked|no-progress> --note '<one line>'",
        "stopped": f"loopctl finish {u} --token {t} --stopped",
    }
    if role == "fix":
        out["pushed"] = f"loopctl event {u} --token {t} fix-pushed --pr <number> --head <sha>"
        out["fixed"] = f"loopctl event {u} --token {t} finding-fixed --finding <f-id> --commit <sha> --test '<test>'"
    elif role == "review":
        head = (src.pr or {}).get("head") or "<head sha>"
        out["submit"] = f"loopctl review submit {u} --token {t} --head {head} --file /tmp/findings.json"
    elif role in ("gate", "check"):
        out["gate"] = f"loopctl gate {u} --token {t}"
    elif role == "revert":
        rv = src.revert or {}
        red, good, b = rv.get("red", "<red sha>"), rv.get("good", "<good sha>"), branch("revert", u)
        out["revert"] = (f"git fetch origin {src.main} && git checkout -B {b} {red} && git read-tree -u --reset {good} "
                         f"&& git commit --no-verify -m 'Revert main to {good[:12]}: the post-merge check failed on "
                         f"{red[:12]}'")  # fmt: skip
        out["push"] = f"git push origin {b}"
        out["pushed"] = f"loopctl event {u} --token {t} revert-pushed --pr <number> --head <sha>"
    elif role == "triage":
        out["triage"] = (f"loopctl event {u} --token {t} triage-done --verdict <accepted|rejected|deferred|duplicate> "
                         "--priority <P0|P1|P2|P3> [--duplicate-of <number>] [--frozen]")  # fmt: skip
    elif role == "reconcile":  # the comparison runs in code (`loopctl reconcile`); the brief has a fixed size
        out["reconcile"] = f"loopctl reconcile --out {RECONCILE_FILE}"
        out["report"] = f"loopctl event {u} --token {t} reconcile-report --file {RECONCILE_FILE}"
    return out


# --- sections ---------------------------------------------------------------------------------------------------------


def as_data(label: str, text: str) -> str:
    """GitHub text, fenced as data. The fence words inside it are defused so the text can't close the fence."""
    clean = text.replace("DATA>>>", "DATA> > >").replace("<<<DATA", "< < <DATA")
    return f"{DATA_OPEN.format(label=label)}\n{clean.rstrip()}\n{DATA_CLOSE}"


def _finding_line(f: Mapping[str, Any]) -> str:
    where = f"{f.get('path', '?')}" + (f":{f['line']}" if f.get("line") else "")
    status = f.get("status", "open")
    head = f"- {f.get('id', '?')} [{f.get('severity', '?')}, {f.get('introduced', '?')}, {status}] {where}"
    body = f"  summary: {f.get('summary', '')}"
    repro = f"  repro: {f['repro']}" if f.get("repro") else ""
    return "\n".join(x for x in (head, body, repro) if x)


def _findings(label: str, findings: Sequence[Mapping[str, Any]]) -> str:
    if not findings:
        return "(none)"
    return as_data(label, "\n".join(_finding_line(f) for f in findings))


def _notes(notes: Sequence[Mapping[str, Any]]) -> str:
    if not notes:
        return "(none)"
    lines = [f"- {n.get('path', '?')}: {n.get('summary', '')} (seen {n.get('times_seen', 1)}×)"
             for n in list(notes)[:NOTES_COUNT]]  # fmt: skip
    return as_data("notes", "\n".join(lines))


def _issue(src: BriefInput) -> tuple[str, str]:
    issue = src.issue or {}
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    labels = ", ".join(str(x) for x in issue.get("labels") or []) or "none"
    return f"Issue #{issue.get('number', '?')}: {title} (labels: {labels})", body


def freeze_text() -> str:
    return (TEMPLATES / "freeze.md").read_text(encoding="utf-8").strip()


def schema_text() -> str:
    """The findings format a review submits, from loopctl/schemas/finding.json (one line per field)."""
    schema = json.loads((Path(__file__).resolve().parent / "schemas" / "finding.json").read_text(encoding="utf-8"))
    lines = []
    for name in schema["required"]:
        spec = schema["properties"][name]
        if name == "id":
            continue  # loopctl assigns ids when it records the submission (an id carries a time: code writes it)
        if "enum" in spec:
            what = " | ".join(spec["enum"])
        elif spec.get("type") == ["integer", "null"]:
            what = "line number or null"
        else:
            what = f"text, at most {spec.get('maxLength', '?')} characters"
        lines.append(f"- `{name}`: {what}")
    return ("A JSON list of findings (an empty list when there are none), each an object with exactly these fields "
            "(no `id`: loopctl assigns it):\n" + "\n".join(lines))


def sections(role: str, src: BriefInput) -> tuple[dict[str, str], list[Section]]:
    """(scalar fields, sections) for a role. Scalars are short fixed facts; sections are budgeted."""
    cmds = commands(role, src)
    fields: dict[str, str] = {"unit": src.unit, "controller_session": src.controller_session or "<controller>"}
    fields |= {f"cmd_{k}": v for k, v in cmds.items()}
    out: list[Section] = []
    freeze = Section("freeze", freeze_text() if src.freeze else "(no feature freeze)", mandatory=False, priority=5)
    if role == "fix":
        heading, body = _issue(src)
        fields["task_heading"] = heading
        out += [
            Section("task", as_data("issue", body or "(empty)"), True, 1, cap=ISSUE_BODY_MAX,
                    why=f"the issue body is over {ISSUE_BODY_MAX} tokens: split or summarise the issue"),
            Section("findings", _findings("findings", [f for f in src.findings if f.get("status") != "fixed"]),
                    True, 2, why="the open blocking findings don't fit the brief: split the PR"),
            freeze,
            Section("notes", _notes(src.notes), False, 6, cap=NOTES_MAX),
        ]
        pr = src.pr or {}
        fields["pr_line"] = (f"This is a fix round on PR #{pr['number']} (branch `{pr.get('branch', '?')}`): push to "
                             "that branch." if pr.get("number") else
                             f"This is the first build: open a PR from a new branch `{branch('fix', src.unit)}` "
                             "(exactly this name: the loop never adopts its own `loop/` branches as separate units)."
                             )  # fmt: skip
    elif role == "review":
        pr = src.pr or {}
        fields |= {"pr_number": str(pr.get("number", "?")), "pr_title": str(pr.get("title") or ""),
                   "head": str(pr.get("head") or "?"), "tier": src.tier or "normal", "round": str(src.round),
                   "round_cap": str(src.round_cap or "?"), "critical": "yes" if src.critical else "no"}  # fmt: skip
        delta = (f"Round {src.round}: the diff below is only the delta since the last reviewed head "
                 f"{src.delta_from}. Re-check the earlier findings, then review the delta."
                 if src.round >= 3 and src.delta_from else "The diff below is the whole PR.")  # fmt: skip
        out += [
            Section("schema", schema_text(), True, 1),
            Section("previous", _findings("previous findings", src.findings), True, 2,
                    why="the previous findings don't fit the review brief: split the PR"),
            Section("subagents", subagent_prompts(src), True, 3),
            Section("delta", delta, True, 4),
            freeze,
            Section("pr_body", as_data("pr", str(pr.get("body") or "(empty)")), False, 6, cap=PR_BODY_MAX),
            Section("diff", as_data("diff", src.diff or "(empty)"), True, 9, cap=DIFF_MAX, extra=True,
                    why=f"the diff is over {DIFF_MAX} tokens: split the PR"),
        ]
    elif role == "reviewer":
        if src.focus not in FOCUSES:
            raise BriefError(f"a reviewer's focus is one of {', '.join(FOCUSES)}, not {src.focus!r}")
        fields["focus"] = src.focus
        out += [
            Section("focus_rules", (TEMPLATES / "roles" / f"focus-{src.focus}.md").read_text(encoding="utf-8").strip(),
                    True, 1),
            Section("schema", schema_text(), True, 2),
            Section("slice", as_data("diff", src.slice or "(empty)"), True, 9, cap=SLICE_MAX, extra=True,
                    why=f"the slice is over {SLICE_MAX} tokens: give the reviewer a smaller part"),
        ]
    elif role == "verifier":
        if not src.finding:
            raise BriefError("a verifier brief needs the finding to disprove")
        out += [
            Section("finding", as_data("finding", _finding_line(src.finding)), True, 1),
            Section("slice", as_data("diff", src.slice or "(empty)"), True, 9, cap=SLICE_MAX, extra=True,
                    why=f"the slice is over {SLICE_MAX} tokens: give the verifier a smaller part"),
        ]
    elif role in ("gate", "check"):
        pass  # nothing to decide: the template is the brief
    elif role == "revert":
        rv = src.revert or {}
        fields |= {"red": rv.get("red", "?"), "good": rv.get("good", "?"), "branch": branch("revert", src.unit),
                   "main": src.main}  # fmt: skip
    elif role == "triage":
        heading, body = _issue(src)
        fields["task_heading"] = heading
        dupes = "\n".join(f"- #{d.get('number')}: {d.get('title', '')} ({d.get('state', '?')})" for d in src.duplicates)
        out += [
            Section("task", as_data("issue", body or "(empty)"), True, 1, cap=TRIAGE_ISSUE_MAX,
                    why=f"the issue body is over {TRIAGE_ISSUE_MAX} tokens: ask its author to summarise it"),
            Section("freeze", freeze_text() if src.freeze else "(no feature freeze)", True, 2),
            Section("duplicates", as_data("search", dupes) if dupes else "(no similar issues found)", False, 3),
        ]
    elif role == "reconcile":
        pass  # no sections: `loopctl reconcile` reads the store and GitHub itself
    else:
        raise BriefError(f"unknown role {role!r}; roles: {', '.join(ROLES)}")
    return fields, out


def subagent_prompts(src: BriefInput) -> str:
    """The reviewer and verifier prompts the review session gives its subagents (engine templates, §8.1), with their
    slots shown as <slot> for the session to fill: it pastes the prompt, then the slice or the finding."""
    def slots(text: str) -> str:
        return PLACEHOLDER.sub(lambda m: f"<{m.group(1)}>", text).strip()

    focus = "\n\n".join(f"Focus `{f}`:\n" + slots((TEMPLATES / "roles" / f"focus-{f}.md").read_text(encoding="utf-8"))
                         for f in FOCUSES)  # fmt: skip
    return (
        "--- reviewer prompt (one subagent per focus: correctness, security, spec; a docs-tier PR gets spec only) ---\n"
        + slots(template("reviewer")) + "\n\n" + focus
        + "\n\n--- verifier prompt (one subagent per finding of severity medium or above) ---\n"
        + slots(template("verifier"))
    )


# --- rendering --------------------------------------------------------------------------------------------------------


def template(role: str) -> str:
    return (TEMPLATES / "roles" / f"{role}.md").read_text(encoding="utf-8")


def _cut_to(text: str, budget: int, name: str) -> str:
    marker = CUT.format(name=name)
    if budget <= tokens(marker) + 1:
        return ""
    keep: list[str] = []
    for line in text.splitlines():
        if tokens("\n".join([*keep, line, marker])) > budget:
            break
        keep.append(line)
    if keep and keep[0].startswith(DATA_OPEN.split(" ")[0]) and DATA_CLOSE not in keep:
        keep.append(DATA_CLOSE)  # never leave a data fence open
        while keep and tokens("\n".join([*keep, marker])) > budget and len(keep) > 2:
            keep.pop(-2)
        if tokens("\n".join([*keep, marker])) > budget:  # the bare fence pair and the marker alone don't fit
            return ""
    return "\n".join([*keep, marker]) if keep else ""


def attach_text(repo: str) -> str:
    """The brief's first step when the worker must attach the target itself (decision 0030): its session starts from
    the state repository only, because `create_session` takes one repository."""
    if not repo:
        return ""
    owner, _, name = repo.partition("/")
    return (f"Before anything else, attach the repository you work on: call the `add_repo` tool with owner `{owner}`, "
            f"repo `{name}` and access `push`. Your session starts in the state repository, where the guard refuses "
            f"every command but `loopctl`, so run the clone command it returns as `cd .. && <clone command>`, which "
            f"puts the checkout in `../{name}`. From then on start every shell command that isn't `loopctl` with "
            f"`cd ../{name} && `. Every step below needs the checkout.\n\n")


def render(role: str, src: BriefInput, read_template: Callable[[str], str] = template) -> Brief:
    if role not in ROLES:
        raise BriefError(f"unknown role {role!r}; roles: {', '.join(ROLES)}")
    budget = BUDGET[role]
    brief = Brief(role, src.unit, budget=budget)
    text = read_template(role)
    fields, secs = sections(role, src)
    used = set(PLACEHOLDER.findall(text))
    missing = used - set(fields) - {s.name for s in secs}
    if missing:
        raise BriefError(f"template {role}.md uses unknown placeholder(s): {', '.join(sorted(missing))}")
    for name, value in fields.items():
        text = text.replace("{{" + name + "}}", value)
    fixed = tokens(PLACEHOLDER.sub("", text))
    room = budget - fixed
    filled: dict[str, str] = {}
    base_secs = [s for s in secs if not s.extra and s.name in used]
    for s in sorted((s for s in base_secs if s.mandatory), key=lambda s: s.priority):
        if s.cap is not None and s.size > s.cap or s.size > room:
            brief.needs_owner = s.why or f"the {s.name} section doesn't fit the {role} brief's {budget} tokens"
            return brief
        filled[s.name] = s.text
        room -= s.size
    for s in sorted((s for s in base_secs if not s.mandatory), key=lambda s: s.priority):
        limit = min(room, s.cap) if s.cap is not None else room
        if s.size <= limit:
            filled[s.name] = s.text
        else:
            filled[s.name] = _cut_to(s.text, limit, s.name)
            brief.cut.append(s.name)
        room -= tokens(filled[s.name])
    brief.base_tokens = budget - room
    for s in (s for s in secs if s.extra and s.name in used):
        if s.cap is not None and s.size > s.cap:
            brief.needs_owner = s.why
            return brief
        filled[s.name] = s.text
    for name, value in filled.items():
        text = text.replace("{{" + name + "}}", value)
    brief.text = attach_text(src.attach) + PLACEHOLDER.sub("", text).strip() + "\n"
    brief.tokens = tokens(brief.text)
    return brief


# --- the command line -------------------------------------------------------------------------------------------------


def add_arguments(parser: Any) -> None:
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("unit", help="issue-12, pr-40, reconcile-20261118 …")
    parser.add_argument("--input", required=True, help="a JSON file with the brief's input (built by the tick)")
    parser.add_argument("--focus", choices=FOCUSES, help="reviewer: which focus")
    parser.add_argument("--finding", help="verifier: the id of the finding to disprove (from the input's findings)")
    parser.add_argument("--json", action="store_true", help="print the brief and its accounting as JSON")


def run(args: Any) -> int:
    try:
        data = json.loads(Path(args.input).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise BriefError("the brief input must be a JSON object")
        data = {**data, "unit": args.unit}
        if args.focus:
            data["focus"] = args.focus
        if args.finding:
            match = [f for f in data.get("findings") or [] if f.get("id") == args.finding]
            if not match:
                raise BriefError(f"no finding {args.finding} in the input")
            data["finding"] = match[0]
        brief = render(args.role, BriefInput.from_json(data))
    except (OSError, ValueError, TypeError) as exc:
        print(f"loopctl brief: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(brief.as_dict() | {"text": brief.text}, ensure_ascii=False, indent=1))
    elif brief.needs_owner is None:
        sys.stdout.write(brief.text)
    if brief.needs_owner is not None:
        print(f"loopctl brief: needs_owner: {brief.needs_owner}", file=sys.stderr)
        return 3
    return 0
