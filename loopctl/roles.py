"""Role defaults: the model, effort and permission mode each kind of session gets when `.agent-loop.toml` and the
owner's commands leave `models.<role>.model|effort` empty (build-plan G5, M0.13).

Why these values:
- **controller: Sonnet** (owner, 2026-11-17; phase2-design §4.2). The owner picks it in the session form; the tick's
  self-check compares it with `models.controller.model` (whose engine default is the same id). Its permission mode is
  the owner's choice in the form, so none is set here.
- **fix, build, review, reviewer, verifier: Opus.** Every Phase 1 fix worker ran on Opus, and "Sonnet for mechanical
  fixes" was dropped (K12; phase1-diagnosis Hypothesis 2: untestable, small upside).
- **gate (also check and revert): Sonnet, low effort.** A gate session runs one command and records its result; the
  judgement is in code (`loopctl gate`, `verdict.check_merge`).
- **triage (also reconcile): Sonnet, medium effort.** Classification against fixed rules.
- **permission_mode `auto` for every worker:** nobody is there to answer a permission prompt, and the live loop's
  workers ran in auto mode (phase3-step1-spikes S1, A2). The guard hook (PreToolUse, installed by the worker
  environment's setup script) is the enforcement; the platform's classifier stays as a second check.

`effort = ""` means the platform default. These defaults are applied where sessions are started (`loopctl tick`'s
`create_session` actions), not inside the config resolution, so `config_hash` and the page's fixtures don't move.
"""

from __future__ import annotations

from typing import Any

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"
WORKER_MODE = "auto"

DEFAULTS: dict[str, dict[str, str | None]] = {
    "controller": {"model": SONNET, "effort": "", "permission_mode": None},
    "triage": {"model": SONNET, "effort": "medium", "permission_mode": WORKER_MODE},
    "fix": {"model": OPUS, "effort": "high", "permission_mode": WORKER_MODE},
    "build": {"model": OPUS, "effort": "high", "permission_mode": WORKER_MODE},
    "review": {"model": OPUS, "effort": "high", "permission_mode": WORKER_MODE},
    "reviewer": {"model": OPUS, "effort": "high", "permission_mode": WORKER_MODE},
    "verifier": {"model": OPUS, "effort": "high", "permission_mode": WORKER_MODE},
    "gate": {"model": SONNET, "effort": "low", "permission_mode": WORKER_MODE},
}


def session_settings(role: str, cfg: Any) -> dict[str, str | None]:
    """{model, effort, permission_mode} for a session of `role` (a `models.*` role): the effective config's value
    where it is set, else the role default. Unknown roles get the worker defaults of `fix`."""
    from loopctl.risk import _get

    base = DEFAULTS.get(role, DEFAULTS["fix"])
    def get(key: str) -> Any:
        try:
            return _get(cfg, f"models.{role}.{key}")
        except (KeyError, ValueError):  # a role without its own models.* keys (ConfigError is a ValueError)
            return None

    model = get("model") or base["model"]
    effort = get("effort") or base["effort"]
    return {"model": model or None, "effort": effort or None, "permission_mode": base["permission_mode"]}
