"""First-call claim of a unit lease (decision record 0029, M0.13 L-DRILL d).

The lease token (0029) proves a session saw the unit's brief, but a duplicate start of the same unit (the controller
calling `create_session` twice with one prompt) gives two sessions the same brief and so the same token. The claim
binds the lease to one session: the worker's **first** unit-scoped `loopctl` call (in practice `loopctl guard
identify <kind> <unit> --token T`, the first command of every brief; else the first `loopctl event|finish|gate|review
submit`) writes `claims/<lease_token_sha256>.json` in the state repo, in the same compare-and-swap commit as its
events. Every later unit-scoped call with that token must come from the same claimant; any other claimant is refused
before anything is written, and the guard then holds that session stopped (only `loopctl finish --stopped`, which
also records nothing for it, and the end of the session remain).

The claimant is who the calling session is, from the container it runs in:

1. `CLAUDE_CODE_SESSION_ID`: Claude Code sets it in every session's environment (seen in cloud sessions, 2026-11-18,
   as a UUID). It is not the `session_…` id that `create_session` returns (a different id space, no known mapping),
   so the tick's `lease.bound` and the claim are not compared; the claim needs no comparison, only "first one wins".
2. Else a random 128-bit id kept in `~/.agent-loop/claim` (`$AGENT_LOOP_HOME/claim`), made once per container
   (written whole, then linked into place: the first process wins). A cloud session has its own container, so a
   duplicate session gets its own id.

Only the claimant's sha256 is stored. Exactly one of two racing first calls wins: both see no claim file, both
commit, one push is rejected (store.py's CAS), and the loser decides again on the fresh store and sees the winner's
claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loopctl import clock

CLAIMS_DIR = "claims"
SESSION_VAR = "CLAUDE_CODE_SESSION_ID"
CLAIM_FILE = "claim"  # under the agent-loop home: the fallback claimant id
REFUSED_FILE = "claim-refused"  # under the agent-loop home: read by the guard (the session is stopped)
_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")


class ClaimError(RuntimeError):
    """The calling session's identity couldn't be established (nothing is written: fail closed)."""


@dataclass(frozen=True)
class Claimant:
    id: str
    source: str  # "CLAUDE_CODE_SESSION_ID" or "claim-file"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.id.encode("utf-8")).hexdigest()


def home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("AGENT_LOOP_HOME") or str(Path(env.get("HOME") or Path.home()) / ".agent-loop"))


def claimant(env: Mapping[str, str] | None = None) -> Claimant:
    """Who is calling: the session id from the environment, else this container's claim id (made once)."""
    env = os.environ if env is None else env
    sid = (env.get(SESSION_VAR) or "").strip()
    if sid:
        if not _ID.match(sid):
            raise ClaimError(f"{SESSION_VAR} isn't a session id: {sid[:40]!r}")
        return Claimant(sid, SESSION_VAR)
    path = home(env) / CLAIM_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # written whole to a temporary file, then linked: os.link fails if another process linked first
            tmp = path.with_name(f".{CLAIM_FILE}-{secrets.token_hex(4)}")
            tmp.write_text("claim-" + secrets.token_hex(16) + "\n", encoding="ascii")
            os.chmod(tmp, 0o600)
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass  # another process of this container made it first: use theirs
            finally:
                tmp.unlink()
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ClaimError(f"{path} can't be read or made: {exc}") from exc
    if not _ID.match(text):
        raise ClaimError(f"{path} doesn't hold a claim id; remove it and try again")
    return Claimant(text, "claim-file")


def path_for(lease: Mapping[str, Any]) -> str:
    """claims/<the lease token's sha256>.json: one claim per lease (a token dies with its lease)."""
    return f"{CLAIMS_DIR}/{lease['lease_token_sha256']}.json"


def holder(text: str | None) -> str | None:
    """The claimant sha256 recorded in a claim file's text (None: no claim yet). An unreadable claim file counts as
    someone else's claim (fail closed)."""
    if text is None:
        return None
    try:
        doc = json.loads(text)
        value = doc["claimant_sha256"]
    except (ValueError, KeyError, TypeError):
        return "unreadable"
    return value if isinstance(value, str) else "unreadable"


def document(unit: str, lease: Mapping[str, Any], who: Claimant) -> str:
    doc = {"unit": unit, "kind": lease["kind"], "attempt": lease["attempt"],
           "lease_token_sha256": lease["lease_token_sha256"], "claimant_sha256": who.sha256,
           "claimant_source": who.source, "claimed_at": clock.stamp()}  # fmt: skip
    return json.dumps(doc, indent=1, sort_keys=True) + "\n"


def taken_reason(unit: str) -> str:
    return (f"another session already claimed {unit}'s lease (a duplicate start of this unit): this session doesn't "
            "hold it. Nothing was recorded. Stop now: push nothing, and end this session")  # fmt: skip


def mark_refused(reason: str, env: Mapping[str, str] | None = None) -> None:
    """Tell this container's guard that the session lost the claim: it then refuses every tool call except
    `loopctl finish --stopped` (guard_stop). A write that fails is ignored: loopctl refuses every call anyway."""
    try:
        target = home(env) / REFUSED_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(reason + "\n", encoding="utf-8")
    except OSError:
        pass
