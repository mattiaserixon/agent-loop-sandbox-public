"""Owner commands: `loopctl ingest-commands` (phase2-design §12.3, §3.3a, §4.5; page/README.md; proof tests T-CMD-1…7).

The page writes command documents with six fields (`loopctl/schemas/command.json`); the controller lists them into a
file and runs `loopctl ingest-commands <file>`. `ingest()` is pure (time and ids come from `loopctl.clock`):

- **Order (P1).** New documents (those without `status`) are handled in send order, `client_time` then id, never in
  file or document-id order. Within one run, new commands are handled before GitHub confirmations, so a confirmation
  that arrives together with a later command on the same setting loses to it (P4, fails towards the narrower
  setting).
- **Classification at its position (P2).** Whether a command widens authority is judged against the effective
  config it is applied to (after every command before it). Widening = raising a number, a window schedule's share
  above its smallest entry, a non-number setting, `resume`, switching to `push`, an answer whose option widens.
  Narrowing commands apply at once; widening ones wait for the owner's GitHub confirmation.
- **Replacement (P3, P4).** Applying a command drops every earlier-sent command still waiting for confirmation on a
  shared setting as "replaced before confirmation"; a confirmation that comes after that is not applied.
- **Exactly once (P5).** A command id already in the store is never handled again (its write-back is repeated); a
  nonce already used by another command is rejected.
- **STOP wins (P7).** While `STOP` holds any line, every widening command is rejected, except `resume`: a confirmed
  `resume` is the one path that removes `STOP` lines (§4.5; the workflow removes them when it records the
  confirmation).
- **Confirmations** are accepted only from commits GitHub reports as verified and authored by the loop's App
  (`install.json` `app_login`), naming the command id and its nonce. The workflow (M0.11) writes them as
  `confirmations/<command id>.json` = `{command_id, nonce, decision: "confirm"|"refuse", comment_url}` in the state
  repo; every commit GitHub lists for that path must be verified and the App's.
- **Pause and stop** also add their line to `STOP` (§4.5: "the controller: a page command it ingests").

The effects reproduce the page's `predict` exactly (P6, tests/test_page_fingerprint.py and tests/test_commands.py):
pause/resume with kinds keep `run.paused_kinds` in `KINDS` order; shares are rounded to 4 places; a mode change
replaces only the preset keys whose value came from the engine or a preset (that is what `config.resolve` does with
the mode as the newest runtime override).

Runtime overrides live in `state.json` `overrides` (one per key, the latest), with the command as their source; the
tick resolves its config from `.agent-loop.toml` plus those overrides (`effective()`), so `config_sources` shows
"command c-… by owner at …" for them.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from loopctl import clock, schema, stop
from loopctl.config import KINDS, MODES, Config, ConfigError, Override, check_override, resolve
from loopctl.store import make_event

CONFIG_TYPES = ("pause", "resume", "mode", "set")
SUPPORTED = (*CONFIG_TYPES, "stop", "answer", "tick_now")
PAGE_FIELDS = ("id", "type", "args", "nonce", "client_time", "expected_hash")
CONTROLLER_FIELDS = ("status", "reason", "tick_id", "received_at", "applied_at", "confirmed_at", "config_before",
                     "config_hash_before", "config_hash_after", "config_sources_before", "confirm_url")  # fmt: skip
RECEIVED, AWAITING, APPLIED, REJECTED, REPLACED = ("received", "awaiting GitHub confirmation", "applied", "rejected",
                                                   "replaced before confirmation")  # fmt: skip
REPLACED_REASON = "a later change to the same setting was applied first; send a new request if you still want it"


# --------------------------------------------------------------------------------------------- the page's rules
def send_key(cmd: Mapping[str, Any]) -> tuple[str, str]:
    return (str(cmd.get("client_time", "")), str(cmd.get("id", "")))


def _get(config: Mapping[str, Any], key: str) -> Any:
    node: Any = config
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _kinds(args: Mapping[str, Any]) -> list[Any]:
    kinds = args.get("kinds")
    return list(kinds) if isinstance(kinds, list) else []


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def widens(cmd: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    """The page's `widens`: whether `cmd` widens authority against `config` (a `Config.as_dict()`)."""
    a = cmd.get("args") or {}
    t = cmd.get("type")
    if t in ("resume", "approve", "waive", "include_label"):
        return True
    if t == "mode":
        return a.get("mode") == "push" and _get(config, "run.mode") != "push"
    if t == "answer":
        return a.get("widens") is True
    if t == "set":
        if a.get("key") == "run.mode":
            return a.get("value") == "push" and _get(config, "run.mode") != "push"
        cur, value = _get(config, str(a.get("key"))), a.get("value")
        if not _number(value):
            return True
        if isinstance(cur, list):
            return value > min(e["share"] for e in cur)
        if _number(cur):
            return value > cur
        return True
    return False


def settings_of(cmd: Mapping[str, Any]) -> list[str]:
    """The settings a command touches (the page's `settingsOf`); two commands on one setting replace each other."""
    a = cmd.get("args") or {}
    t = cmd.get("type")
    if t == "set":
        return [str(a.get("key"))]
    if t == "mode":
        return ["run.mode"]
    if t in ("pause", "resume"):
        kinds = _kinds(a)
        return [f"run.paused_kinds:{k}" for k in kinds] if kinds else ["run.paused"]
    if t == "stop":
        return [f"unit:{a.get('unit')}"]
    return []


def effect(cmd: Mapping[str, Any], cfg: Config) -> list[tuple[str, Any]]:
    """The command alone as (key, value) runtime overrides against `cfg`. Raises ConfigError if it can't apply."""
    a = cmd.get("args") or {}
    t = cmd.get("type")
    if t in ("pause", "resume"):
        kinds = _kinds(a)
        if not kinds:
            return [("run.paused", t == "pause")]
        bad = [k for k in kinds if k not in KINDS]
        if bad:
            raise ConfigError(f"unknown kind(s) {bad}; kinds are {', '.join(KINDS)}")
        now = set(cfg.value("run.paused_kinds"))
        now = now | set(kinds) if t == "pause" else now - set(kinds)
        return [("run.paused_kinds", [k for k in KINDS if k in now])]
    if t == "mode" or (t == "set" and a.get("key") == "run.mode"):
        mode = a.get("mode") if t == "mode" else a.get("value")
        if mode not in MODES:
            raise ConfigError(f"run.mode must be one of {', '.join(MODES)}, got {mode!r}")
        return [("run.mode", mode)]
    if t == "set":
        if not isinstance(a.get("key"), str) or not a["key"] or "value" not in a:
            raise ConfigError("set needs a key and a value")
        return [(a["key"], a["value"])]
    return []


def overrides_of(state: Mapping[str, Any]) -> list[Override]:
    return [Override(o["key"], o["value"], o["source"]) for o in state.get("overrides") or []]


def effective(project_text: str | None, state: Mapping[str, Any], project_file: str = ".agent-loop.toml") -> Config:
    """The tick's config: `.agent-loop.toml` plus the runtime overrides of applied commands."""
    return resolve(project_text, project_file=project_file, commands=overrides_of(state))


def apply_alone(cmd: Mapping[str, Any], project_text: str | None, overrides: Sequence[Override],
                source: str = "command") -> Config:  # fmt: skip
    """The config after `cmd` alone is added to `overrides` (what the page's `predict` must equal)."""
    before = resolve(project_text, commands=overrides)
    extra = [check_override(Override(k, v, source)) for k, v in effect(cmd, before)]
    return resolve(project_text, commands=[*overrides, *extra])


# ------------------------------------------------------------------------------------------------- confirmations
@dataclass(frozen=True)
class Confirmation:
    """A confirmation file in the state repo (`confirmations/<command id>.json`, written by the workflow with the App's
    token) with the commits GitHub lists for it. Accepted only if every commit is verified and authored by the App."""

    command_id: str
    nonce: str
    decision: str  # "confirm" | "refuse"
    commits: tuple[Mapping[str, Any], ...] = ()  # [{sha, verified, author}]
    url: str | None = None  # the owner's comment on the control issue

    def accepted(self, app_login: str | None) -> str | None:
        """None when it can be trusted, else why not."""
        if not app_login:
            return "install.json names no app_login, so no confirmation can be trusted"
        if not self.commits:
            return "GitHub lists no commit for it"
        for c in self.commits:
            if c.get("verified") is not True:
                return f"commit {str(c.get('sha'))[:12]} is not verified by GitHub"
            if c.get("author") != app_login:
                return f"commit {str(c.get('sha'))[:12]} is authored by {c.get('author') or 'nobody'}, not {app_login}"
        if self.decision not in ("confirm", "refuse"):
            return f"unknown decision {self.decision!r}"
        return None


# --------------------------------------------------------------------------------------------------------- ingest
@dataclass
class Result:
    state: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)
    writebacks: dict[str, dict[str, Any]] = field(default_factory=dict)  # command id -> controller-owned fields
    stop_text: str | None = None  # the new STOP text when lines were added, else None
    notices: list[str] = field(default_factory=list)
    tick_id: str = ""


def writeback(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(record[k]) for k in CONTROLLER_FIELDS if k in record}


def stop_line(cmd: Mapping[str, Any]) -> str | None:
    """The STOP line a pause or stop command adds (the page opens GitHub's form with the same line)."""
    a = cmd.get("args") or {}
    if cmd.get("type") == "pause":
        kinds = [k for k in KINDS if k in _kinds(a)]
        return "pause " + " ".join(kinds) if kinds else "pause"
    if cmd.get("type") == "stop" and isinstance(a.get("unit"), str) and stop.UNIT.match(a["unit"]):
        return f"stop {a['unit']}"
    return None


def _validate(doc: Any) -> tuple[dict[str, Any] | None, str | None]:
    """The page's six fields, checked against command.json (controller-owned fields are ignored)."""
    if not isinstance(doc, Mapping):
        return None, "not an object"
    cmd = {k: doc[k] for k in PAGE_FIELDS if k in doc}
    cmd.setdefault("expected_hash", None)
    extra = sorted(set(doc) - set(PAGE_FIELDS) - set(CONTROLLER_FIELDS))
    if extra:
        return cmd, f"unknown field(s) {extra}"
    try:
        schema.validate(cmd, "command")
    except schema.SchemaError as exc:
        return cmd, f"doesn't match the command schema: {exc}"[:300]
    return cmd, None


class _Run:
    def __init__(self, state: Mapping[str, Any], project_text: str | None, project_file: str, stop_text: str | None,
                 confirm_url: str | None, epoch: int | None) -> None:  # fmt: skip
        self.s = copy.deepcopy(dict(state))
        self.s.setdefault("commands", {})
        self.s.setdefault("overrides", [])
        self.project_text, self.project_file = project_text, project_file
        self.stop_text = stop_text
        self.stop_lines = stop.parse(stop_text)
        self.added: list[str] = []
        self.confirm_url, self.epoch = confirm_url, epoch
        self.tick_id = clock.ulid()
        self.now = clock.stamp()
        self.out = Result(self.s, tick_id=self.tick_id)

    # -- helpers
    def cfg(self) -> Config:
        return effective(self.project_text, self.s, self.project_file)

    def event(self, type_: str, payload: dict[str, Any]) -> None:
        self.out.events.append(make_event(type_, payload, writer="controller", epoch=self.epoch))

    def touch(self, rec: dict[str, Any]) -> None:
        self.out.writebacks[rec["id"]] = writeback(rec)

    def reject(self, rec: dict[str, Any], reason: str) -> None:
        rec.update(status=REJECTED, reason=reason[:300], tick_id=self.tick_id)
        self.event("command.rejected", {"command_id": rec["id"], "reason": rec["reason"]})
        self.touch(rec)

    def replace(self, rec: dict[str, Any]) -> None:
        h = self.cfg().hash
        rec.update(status=REPLACED, reason=REPLACED_REASON, tick_id=self.tick_id)
        self.event("command.applied", {"command_id": rec["id"], "config_hash_before": h, "config_hash_after": h,
                                       "verdict": "replaced_before_confirmation"})  # fmt: skip
        self.touch(rec)

    def stopped(self) -> str | None:
        lines = self.stop_lines + [stop.parse_line(x) for x in self.added]
        return lines[0].line if lines else None

    # -- one command
    def handle_new(self, rec: dict[str, Any]) -> None:
        rec.update(status=RECEIVED, tick_id=self.tick_id, received_at=self.now)
        self.event("command.received", {"command": {k: rec[k] for k in PAGE_FIELDS}})
        if rec["type"] not in SUPPORTED:
            return self.reject(rec, f"{rec['type']} commands aren't supported by this engine yet")
        cfg = self.cfg()
        try:
            if rec["type"] in CONFIG_TYPES:
                apply_alone(rec, self.project_text, overrides_of(self.s))  # validates the value now
            elif rec["type"] == "stop" and stop_line(rec) is None:
                raise ConfigError("stop needs a unit such as issue-12")
            elif rec["type"] == "answer" and not self._question(rec):
                raise ConfigError(f"no open question {(rec['args'] or {}).get('question')!r} with that option")
        except ConfigError as exc:
            return self.reject(rec, str(exc))
        if widens(rec, cfg.as_dict()):
            line = self.stopped()
            if line is not None and rec["type"] != "resume":
                return self.reject(rec, f"STOP holds `{line}`: widening commands are refused while the loop is "
                                        "stopped")  # fmt: skip
            rec.update(status=AWAITING)
            if self.confirm_url:
                rec["confirm_url"] = self.confirm_url
            h = cfg.hash
            self.event("command.applied", {"command_id": rec["id"], "config_hash_before": h, "config_hash_after": h,
                                           "verdict": "awaiting_confirmation"})  # fmt: skip
            return self.touch(rec)
        self.apply(rec)

    def _question(self, rec: Mapping[str, Any]) -> Mapping[str, Any] | None:
        a = rec.get("args") or {}
        q = (self.s.get("questions") or {}).get(a.get("question"))
        if q and a.get("option") in {o["id"] for o in q["options"]}:
            return q
        return None

    def apply(self, rec: dict[str, Any], confirmed: bool = False) -> None:
        cfg = self.cfg()
        before = cfg.as_dict()
        if rec["type"] in CONFIG_TYPES:
            source = f"command {rec['id']} by owner at {self.now}"
            try:
                new = [check_override(Override(k, v, source)) for k, v in effect(rec, cfg)]
            except ConfigError as exc:
                return self.reject(rec, str(exc))
            keep = [o for o in self.s["overrides"] if o["key"] not in {n.key for n in new}]
            self.s["overrides"] = keep + [{"key": n.key, "value": n.value, "source": n.source, "command": rec["id"]}
                                          for n in new]  # fmt: skip
            after = self.cfg()
            rec.update(config_before=before, config_hash_before=cfg.hash, config_hash_after=after.hash)
            if rec["type"] == "mode" or (rec["type"] == "set" and (rec["args"] or {}).get("key") == "run.mode"):
                rec["config_sources_before"] = {k: st.source for k, st in sorted(cfg.settings.items())}
        elif rec["type"] == "answer":
            if not self._answer(rec):
                return None
        line = stop_line(rec)
        if line is not None and line not in [x.line for x in self.stop_lines] + self.added:
            self.added.append(line)
        rec.update(status=APPLIED, tick_id=self.tick_id, applied_at=self.now)
        rec.pop("reason", None)
        if confirmed:
            rec["confirmed_at"] = self.now
        h_after = rec.get("config_hash_after") or cfg.hash
        self.event("command.applied", {"command_id": rec["id"], "config_hash_before": cfg.hash,
                                       "config_hash_after": h_after, "verdict": "in_effect"})  # fmt: skip
        self.touch(rec)
        mine = set(settings_of(rec))
        for other in self.s["commands"].values():
            if (other["status"] == AWAITING and other["id"] != rec["id"] and send_key(other) < send_key(rec)
                    and mine & set(settings_of(other))):  # fmt: skip
                self.replace(other)

    def _answer(self, rec: dict[str, Any]) -> bool:
        from loopctl.reducer import Policy, reduce

        q = self._question(rec)
        if q is None:
            self.reject(rec, "the question is no longer open")
            return False
        a = rec["args"]
        ev = make_event("question.answered", {"command_id": rec["id"], "option": a["option"],
                                              "question_id": a["question"]},
                        writer="controller", unit=q.get("unit"), epoch=self.epoch)  # fmt: skip
        res = reduce(self.s, [ev], Policy(cfg=self.cfg()))
        if res.rejected:
            self.reject(rec, res.rejected[0][1])
            return False
        for k, v in res.state.items():  # the reducer's copy; keep our own command records and overrides
            if k not in ("commands", "overrides"):
                self.s[k] = v
        self.out.events.append(ev)
        return True

    def confirm(self, c: Confirmation, app_login: str | None) -> None:
        rec = self.s["commands"].get(c.command_id)
        if rec is None or rec["status"] != AWAITING:
            return
        why = c.accepted(app_login)
        if why is None and c.nonce != rec["nonce"]:
            why = "its nonce isn't this command's"
        if why is not None:
            self.out.notices.append(f"confirmation for {c.command_id} not accepted: {why}")
            return
        mine = set(settings_of(rec))
        if any(o["status"] == APPLIED and send_key(o) > send_key(rec) and mine & set(settings_of(o))
               for o in self.s["commands"].values()):  # fmt: skip
            return self.replace(rec)
        if c.decision == "refuse":
            rec["confirmed_at"] = self.now
            return self.reject(rec, "refused on GitHub")
        line = self.stopped()
        if line is not None and rec["type"] != "resume":
            return self.reject(rec, f"STOP holds `{line}`: widening commands are refused while the loop is stopped")
        if c.url:
            rec["confirm_url"] = c.url
        self.apply(rec, confirmed=True)


def ingest(state: Mapping[str, Any], docs: Iterable[Any], *, project_text: str | None = None,
           project_file: str = ".agent-loop.toml", confirmations: Sequence[Confirmation] = (),
           app_login: str | None = None, stop_text: str | None = None, confirm_url: str | None = None,
           epoch: int | None = None) -> Result:  # fmt: skip
    """Handle new command documents, then GitHub confirmations. Pure apart from the clock."""
    run = _Run(state, project_text, project_file, stop_text, confirm_url, epoch)
    cmds = run.s["commands"]
    nonces = {r["nonce"]: r["id"] for r in cmds.values() if "nonce" in r}
    new: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for doc in docs:
        if isinstance(doc, Mapping) and doc.get("status"):
            continue  # only documents without `status` are new
        cmd, problem = _validate(doc)
        cid = cmd.get("id") if cmd and isinstance(cmd.get("id"), str) else None
        if cid in cmds:
            run.touch(cmds[cid])  # already handled: repeat its write-back, change nothing (P5)
            continue
        if cid is None or cid in seen_ids or (cmd and problem and "$.id" in problem):
            run.out.notices.append(f"ignored a command document without a usable id: {problem or 'duplicate id'}")
            continue
        seen_ids.add(cid)
        new.append(dict(cmd, _problem=problem))
    for rec in sorted(new, key=send_key):
        problem = rec.pop("_problem")
        rec.setdefault("args", {})
        cmds[rec["id"]] = rec
        if problem:
            rec.update(type=rec.get("type") if isinstance(rec.get("type"), str) else "unknown",
                       client_time=str(rec.get("client_time", "")), nonce=str(rec.get("nonce", "")))  # fmt: skip
            rec.update(status=RECEIVED, tick_id=run.tick_id, received_at=run.now)
            run.reject(rec, problem)
            continue
        if rec["nonce"] in nonces:
            rec.update(status=RECEIVED, tick_id=run.tick_id, received_at=run.now)
            run.reject(rec, f"nonce already used by {nonces[rec['nonce']]}")
            continue
        nonces[rec["nonce"]] = rec["id"]
        run.handle_new(rec)
    for c in sorted(confirmations, key=lambda c: send_key(cmds.get(c.command_id) or {"id": c.command_id})):
        run.confirm(c, app_login)
    if run.added:
        base = (stop_text or "").rstrip("\n")
        run.out.stop_text = (base + "\n" if base else "") + "\n".join(run.added) + "\n"
    run.out.state = run.s
    return run.out


# ------------------------------------------------------------------------------------------- STOP restoration
def resume_covers(cmd: Mapping[str, Any], line: str) -> bool:
    """Whether a confirmed `resume` may have removed `line` (§4.5): no kinds covers every line; kinds cover the
    `pause <kinds>` lines whose kinds it all resumed."""
    kinds = _kinds(cmd.get("args") or {})
    if not kinds:
        return True
    parsed = stop.parse_line(line)
    return bool(parsed and parsed.valid and parsed.kinds and set(parsed.kinds) <= set(kinds))


def removed_lines(state: Mapping[str, Any], stop_text: str | None) -> list[str]:
    """Lines seen at the last tick that are gone now and no confirmed `resume` applied since covers."""
    seen = state.get("stop_seen") or {}
    now = [x.line for x in stop.parse(stop_text)]
    since = seen.get("at") or ""
    resumes = [r for r in (state.get("commands") or {}).values()
               if r.get("type") == "resume" and r.get("status") == APPLIED and r.get("confirmed_at")
               and (r.get("applied_at") or "") >= since]  # fmt: skip
    return [line for line in seen.get("lines") or []
            if line not in now and not any(resume_covers(r, line) for r in resumes)]


# ------------------------------------------------------------------------------------------------------ the shell
CONFIRM_DIR = "confirmations"


def documents(raw: Any) -> list[Any]:
    """The command documents in an `ArtifactData list commands` result: a list, `{documents|docs|items: [...]}`, or
    `{id: document}`."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, Mapping):
        for k in ("documents", "docs", "items", "results"):
            if isinstance(raw.get(k), list):
                return raw[k]
        if all(isinstance(v, Mapping) for v in raw.values()):
            return [dict(v, id=v.get("id", k)) for k, v in raw.items()]
    raise ValueError("the commands file is not a list of command documents")


def read_confirmations(state: Mapping[str, Any], read_file: Any, reader: Any) -> list[Confirmation]:
    """Confirmation files for commands waiting in `state`, each with the commits GitHub lists for it."""
    import json

    out = []
    for cid, rec in sorted((state.get("commands") or {}).items()):
        if rec.get("status") != AWAITING:
            continue
        path = f"{CONFIRM_DIR}/{cid}.json"
        text = read_file(path)
        if text is None:
            continue
        try:
            doc = json.loads(text)
        except ValueError:
            continue
        if not isinstance(doc, Mapping) or doc.get("command_id") != cid:
            continue
        commits: list[dict[str, Any]] = []
        if reader is not None:
            reply = reader.get("/commits", {"path": path, "per_page": 30})
            if reply.status == 200 and isinstance(reply.data, list):
                for c in reply.data:
                    commits.append({"sha": c.get("sha"),
                                    "verified": ((c.get("commit") or {}).get("verification") or {}).get("verified"),
                                    "author": (c.get("author") or {}).get("login")})  # fmt: skip
        out.append(Confirmation(cid, str(doc.get("nonce", "")), str(doc.get("decision", "confirm")), tuple(commits),
                                doc.get("comment_url") if isinstance(doc.get("comment_url"), str) else None))
    return out


def run(store: Any, *, docs: list[Any], project_text: str | None, project_file: str, reader: Any | None,
        scratch: Any, out_file: Any) -> Result:  # fmt: skip
    """`loopctl ingest-commands`: fenced by the controller lease the last tick recorded, one CAS write, then the
    write-back actions printed for the controller session."""
    import json

    from loopctl import tick
    from loopctl.store import Write

    me = tick._read_json(scratch / "controller.json", None)
    if not me or not me.get("session") or not me.get("epoch"):
        raise ValueError("no controller lease recorded in this session yet: run `loopctl tick` first")
    result: dict[str, Result] = {}

    def decide(state: dict[str, Any], st: Any) -> Write | None:
        install = tick._read_json(st.path / tick.INSTALL_FILE, None) or {}
        target, issue = resolve(project_text, project_file=project_file).value("project.repo"), install.get("control_issue")
        # Under 0028 D the controller can't comment, so the confirmation link is the control issue itself: the owner
        # replies `/loop confirm <id> <nonce>` there (the page knows the nonce it wrote).
        url = f"https://github.com/{target}/issues/{issue}" if target and isinstance(issue, int) else None
        res = ingest(state, docs, project_text=project_text, project_file=project_file,
                     confirmations=read_confirmations(state, st.read_file, reader), app_login=install.get("app_login"),
                     stop_text=st.read_file(stop.FILE), confirm_url=url, epoch=me["epoch"])  # fmt: skip
        result["res"] = res
        files = {stop.FILE: res.stop_text} if res.stop_text is not None else {}
        return Write(state=res.state, events=res.events, files=files)

    store.controller_transact(me["session"], me["epoch"], decide, "loopctl ingest-commands")
    res = result["res"]
    actions = []
    for n, (cid, fields) in enumerate(sorted(res.writebacks.items()), start=1):
        path = scratch / f"command-{cid}.json"
        path.write_text(json.dumps(fields, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        actions.append({"id": str(n), "do": "update_command", "args": {"doc": f"commands/{cid}", "file_path": str(path)},
                        "ack": None})  # fmt: skip
    out_file.write(json.dumps({"tick_id": res.tick_id, "notices": res.notices, "do_exactly": actions}, indent=1,
                              ensure_ascii=False) + "\n")  # fmt: skip
    return res
