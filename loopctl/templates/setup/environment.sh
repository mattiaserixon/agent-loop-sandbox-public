#!/bin/bash
# agent-loop __ROLE__ environment setup (agent-loop v2, M0.13; rendered by `loopctl install setup-script`).
# Paste this whole text into the environment's "Setup script" field. It runs when a session's container starts.
#
# What it does, and nothing else:
# 1. Installs loopctl from __ENGINE_REPO__ at the pinned commit __ENGINE_REF__ into ~/.agent-loop/engine
#    (from GitHub; if that fails, later from the attached __ENGINE_REPO__ checkout, at the first tool call).
# 2. Writes ~/.agent-loop/role.json (role, repositories, pinned commit) and links `loopctl` onto the PATH.
# 3. Installs `loopctl guard --role __ROLE__` as a PreToolUse hook for every tool (no matcher) in the user settings,
#    ~/.claude/settings.json, replacing any earlier agent-loop hook and keeping every other setting.
# The hook fails closed: if the engine is missing, at another commit, or crashes, every tool call is refused.
set -u

ROLE=__ROLE__
ENGINE_REPO=__ENGINE_REPO__
ENGINE_REF=__ENGINE_REF__
TARGET_REPO=__TARGET_REPO__
STATE_REPO=__STATE_REPO__
MAIN=__MAIN__

HOME_DIR="${AGENT_LOOP_HOME:-$HOME/.agent-loop}"
ENGINE_DIR="$HOME_DIR/engine"
BIN_DIR="$HOME_DIR/bin"
SETTINGS="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json"
mkdir -p "$BIN_DIR" "$(dirname "$SETTINGS")"

# --- 1. role.json: who this container is (read by loopctl and the guard to find the attached checkouts) ---------
cat > "$HOME_DIR/role.json" <<EOF
{"role": "$ROLE", "engine_repo": "$ENGINE_REPO", "engine_ref": "$ENGINE_REF", "engine_dir": "$ENGINE_DIR",
 "target": "$TARGET_REPO", "state": "$STATE_REPO", "main": "$MAIN"}
EOF

# --- 2. the installer: GitHub, then the attached checkout of the engine repository, then a vendored copy -----------
cat > "$BIN_DIR/agent-loop-install" <<EOF
#!/bin/bash
# The repositories whose checkouts may hold a vendored copy of the engine (<checkout>/loopctl with VENDOR.json):
# only the state and target repositories, which the owner controls (decision 0030).
VENDOR_REPOS="$STATE_REPO $TARGET_REPO"
EOF
cat >> "$BIN_DIR/agent-loop-install" <<'EOF'
# Installs the pinned engine into ENGINE_DIR. Exit 0 only when ENGINE_DIR is at ENGINE_REF: a git checkout at that
# commit, or a vendored copy of it (marker .engine-ref, every file checked against its VENDOR.json).
# Usage: agent-loop-install ENGINE_DIR ENGINE_REPO ENGINE_REF [SECONDS per fetch, default 60]
set -u
ENGINE_DIR="$1" ENGINE_REPO="$2" ENGINE_REF="$3" WAIT="${4:-60}"
at_ref() {
  [ "$(git -C "$ENGINE_DIR" rev-parse HEAD 2> /dev/null)" = "$ENGINE_REF" ] && return 0
  [ "$(cat "$ENGINE_DIR/.engine-ref" 2> /dev/null)" = "$ENGINE_REF" ] && [ -f "$ENGINE_DIR/loopctl/VENDOR.json" ]
}
repo_of() {  # 0 when checkout $1's origin is repository $2
  local origin
  origin=$(git -C "$1" config --get remote.origin.url 2> /dev/null) || return 1
  case "$origin" in */"$2" | */"$2".git | */"$2"/ | *:"$2" | *:"$2".git) return 0 ;; esac
  return 1
}
at_ref && exit 0
if command -v flock > /dev/null; then  # one installer at a time (hooks of parallel tool calls run together)
  mkdir -p "$(dirname "$ENGINE_DIR")"
  exec 9> "$ENGINE_DIR.lock"
  flock -w $((WAIT * 2 + 5)) 9 || exit 1
  at_ref && exit 0
fi
fresh() { rm -rf "$ENGINE_DIR" && git init -q "$ENGINE_DIR"; }
url="${AGENT_LOOP_ENGINE_URL:-https://github.com/$ENGINE_REPO}"
if fresh && timeout "$WAIT" git -C "$ENGINE_DIR" fetch -q --depth 1 "$url" "$ENGINE_REF" 2> /dev/null \
  && git -C "$ENGINE_DIR" checkout -q --detach FETCH_HEAD && at_ref; then
  exit 0
fi
# The attached checkout of the engine repository (the platform's git access), wherever the session cloned it.
# shellcheck disable=SC2086  # AGENT_LOOP_REPOS_ROOT may name several roots, separated by spaces
for root in ${AGENT_LOOP_REPOS_ROOT:-} "$PWD" "$(dirname "$PWD")" "$HOME" /home/user /workspace /workspaces /repos; do
  for co in "$root" "$root"/*; do
    [ -d "$co/.git" ] || continue
    origin=$(git -C "$co" config --get remote.origin.url 2> /dev/null) || continue
    case "$origin" in */"$ENGINE_REPO" | */"$ENGINE_REPO".git | */"$ENGINE_REPO"/ | *:"$ENGINE_REPO" | *:"$ENGINE_REPO".git) ;; *) continue ;; esac
    git -C "$co" cat-file -e "$ENGINE_REF^{commit}" 2> /dev/null \
      || timeout "$WAIT" git -C "$co" fetch -q --update-shallow origin "$ENGINE_REF" 2> /dev/null || continue
    git -C "$co" update-ref refs/agent-loop/pinned "$ENGINE_REF" 2> /dev/null || continue
    if fresh && git -C "$ENGINE_DIR" fetch -q "$co" refs/agent-loop/pinned \
      && git -C "$ENGINE_DIR" checkout -q --detach FETCH_HEAD && at_ref; then
      exit 0
    fi
  done
done
# A vendored copy in a checkout of the state or target repository (a worker session starts from one repository only,
# so the engine travels with it): copied only when every file matches its VENDOR.json and that names ENGINE_REF.
# shellcheck disable=SC2086  # AGENT_LOOP_REPOS_ROOT may name several roots, separated by spaces
for root in ${AGENT_LOOP_REPOS_ROOT:-} "$PWD" "$(dirname "$PWD")" "$HOME" /home/user /workspace /workspaces /repos; do
  for co in "$root" "$root"/*; do
    [ -f "$co/loopctl/VENDOR.json" ] && [ -d "$co/.git" ] || continue
    ok=""
    for repo in $VENDOR_REPOS; do repo_of "$co" "$repo" && ok=1; done
    [ -n "$ok" ] || continue
    python3 -I -B "$co/loopctl/bin/loopctl" install check-vendor "$co/loopctl" --engine-ref "$ENGINE_REF" > /dev/null 2>&1 \
      || continue
    rm -rf "$ENGINE_DIR" && mkdir -p "$ENGINE_DIR" && cp -R "$co/loopctl" "$ENGINE_DIR/loopctl" || continue
    if python3 -I -B "$ENGINE_DIR/loopctl/bin/loopctl" install check-vendor "$ENGINE_DIR/loopctl" \
      --engine-ref "$ENGINE_REF" > /dev/null 2>&1; then
      printf '%s\n' "$ENGINE_REF" > "$ENGINE_DIR/.engine-ref"
      exit 0
    fi
  done
done
exit 1
EOF
chmod 755 "$BIN_DIR/agent-loop-install"

# --- 3. the hook: fails closed (exit 2 refuses the tool call) -------------------------------------------------------
cat > "$BIN_DIR/agent-loop-guard" <<EOF
#!/bin/bash
# agent-loop guard hook ($ROLE). Any failure refuses the tool call (exit 2).
ENGINE_DIR="$ENGINE_DIR"
if [ "\$(git -C "\$ENGINE_DIR" rev-parse HEAD 2> /dev/null)" != "$ENGINE_REF" ]; then
  "$BIN_DIR/agent-loop-install" "\$ENGINE_DIR" "$ENGINE_REPO" "$ENGINE_REF" 20 > /dev/null 2>&1 || {
    echo "agent-loop guard: the engine isn't installed at $ENGINE_REF, so every tool call is refused; restart the session or fix the environment's setup script" >&2
    exit 2
  }
fi
python3 -I "\$ENGINE_DIR/loopctl/bin/loopctl" guard --role $ROLE --target "$TARGET_REPO" --state "$STATE_REPO" \\
  --main "$MAIN" --state-dir auto --engine-repo "$ENGINE_REPO" --engine-dir auto
rc=\$?
if [ "\$rc" -ne 0 ] && [ "\$rc" -ne 2 ]; then
  echo "agent-loop guard: the guard failed (exit \$rc), so the call is refused" >&2
fi
[ "\$rc" -eq 0 ] || exit 2
EOF
chmod 755 "$BIN_DIR/agent-loop-guard"

# --- 4. loopctl on the PATH -------------------------------------------------------------------------------------------
cat > "$BIN_DIR/loopctl" <<EOF
#!/bin/bash
if [ "\$(git -C "$ENGINE_DIR" rev-parse HEAD 2> /dev/null)" != "$ENGINE_REF" ]; then
  "$BIN_DIR/agent-loop-install" "$ENGINE_DIR" "$ENGINE_REPO" "$ENGINE_REF" > /dev/null 2>&1 || {
    echo "loopctl: the engine isn't installed at $ENGINE_REF (see the environment's setup script)" >&2
    exit 1
  }
fi
exec python3 -I "$ENGINE_DIR/loopctl/bin/loopctl" "\$@"
EOF
chmod 755 "$BIN_DIR/loopctl"
linked=""
# shellcheck disable=SC2086  # AGENT_LOOP_LINK_DIRS (tests) may name several directories, separated by spaces
for dir in ${AGENT_LOOP_LINK_DIRS:-/usr/local/bin "$HOME/.local/bin"}; do
  if mkdir -p "$dir" 2> /dev/null && ln -sf "$BIN_DIR/loopctl" "$dir/loopctl" 2> /dev/null; then
    linked="$dir/loopctl"
    break
  fi
  if [ "$dir" = /usr/local/bin ] && sudo -n ln -sf "$BIN_DIR/loopctl" "$dir/loopctl" 2> /dev/null; then
    linked="$dir/loopctl"
    break
  fi
done

# --- 5. the PreToolUse hook in the user settings ----------------------------------------------------------------------
python3 - "$SETTINGS" "$BIN_DIR/agent-loop-guard" "$ROLE" <<'EOF'
import json, os, sys
path, command, role = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, encoding="utf-8") as f:
        settings = json.load(f)
except (OSError, ValueError):
    settings = {}
if not isinstance(settings, dict):
    settings = {}
hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
def ours(group):
    text = json.dumps(group).lower()
    return "agent-loop" in text or "m0spike" in text
kept = [g for g in hooks.get("PreToolUse") or [] if isinstance(g, dict) and not ours(g)]
kept.append({"hooks": [{"type": "command", "command": command, "timeout": 120}]})
hooks["PreToolUse"] = kept
settings["hooks"] = hooks
# The controller schedules its own hourly wake-up (create_trigger), which auto mode refuses without an allow rule;
# the owner chose to allow it in the controller environment only (2026-11-20, decision 0030).
perms = settings.get("permissions") if isinstance(settings.get("permissions"), dict) else {}
allow = [a for a in perms.get("allow") or [] if isinstance(a, str)]
# A worker attaches the target itself (create_session takes one repository), which auto mode refuses without an allow
# rule; the owner chose to allow add_repo for workers only (2026-11-20, decision 0030). The guard still decides what a
# worker may do with an attached repository (pushes only to the target's branches).
wanted = {"controller": "mcp__claude-code-remote__create_trigger", "worker": "mcp__claude-code-remote__add_repo"}
if wanted.get(role) and wanted[role] not in allow:
    allow.append(wanted[role])
if allow:
    perms["allow"] = allow
    settings["permissions"] = perms
tmp = path + ".agent-loop.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
EOF

# --- 6. install the engine now (the hook retries at the first tool call if GitHub isn't reachable yet) --------------
if "$BIN_DIR/agent-loop-install" "$ENGINE_DIR" "$ENGINE_REPO" "$ENGINE_REF"; then
  echo "agent-loop: loopctl $ENGINE_REF installed for the $ROLE (${linked:-not linked onto the PATH: use $BIN_DIR/loopctl})"
else
  echo "agent-loop: the engine couldn't be fetched yet; the guard installs it at the first tool call or refuses every call"
fi
exit 0
