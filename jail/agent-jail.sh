#!/usr/bin/env bash
# agent-jail.sh - run an Agent Jobs command with NO internet.
#
# The command can reach only:
#   * the local Basalt API, as http://127.0.0.1:8080 inside the jail
#     (bridged through a private unix socket, nothing else on the host is reachable)
#   * its own project folder (read-write) and the toolchains listed below (read-only)
# It cannot see ~/.ssh, browser profiles, keyrings, API keys, ~/.config, other repos,
# the job board (8090), the LAN or the internet.
#
# Usage:
#   agent-jail.sh --project /home/jason/agent-work/myrepo [--ro DIR]... [--rw DIR]... -- CMD [ARGS...]
#
# Requires: bubblewrap, socat   (sudo apt install bubblewrap socat)
# Status: DRAFT 2026-11-11, written by Claude, to be tested on Ubuntu 26.04.1 with selftest.sh.

set -euo pipefail

BASALT_HOST=127.0.0.1
BASALT_PORT=8080
JAIL_HOME=/home/agent           # fake, empty home inside the jail

PROJECT=""
RO_DIRS=()
RW_DIRS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT=$(realpath "$2"); shift 2 ;;
    --ro)      RO_DIRS+=("$(realpath "$2")"); shift 2 ;;
    --rw)      RW_DIRS+=("$(realpath "$2")"); shift 2 ;;
    --)        shift; break ;;
    *) echo "agent-jail: unknown option $1" >&2; exit 2 ;;
  esac
done
[[ -n "$PROJECT" && -d "$PROJECT" ]] || { echo "agent-jail: --project DIR is required" >&2; exit 2; }
[[ $# -gt 0 ]] || { echo "agent-jail: no command given" >&2; exit 2; }

# Refuse obviously dangerous project folders.
case "$PROJECT" in
  "$HOME"|"$HOME/"|/|/home|"$HOME/.ssh"*|"$HOME/.config"*|"$HOME/.mozilla"*|"$HOME/snap"*|"$HOME/.local"*)
    echo "agent-jail: refusing project folder $PROJECT" >&2; exit 2 ;;
esac

command -v bwrap >/dev/null || { echo "agent-jail: bubblewrap not installed" >&2; exit 3; }
command -v socat >/dev/null || { echo "agent-jail: socat not installed" >&2; exit 3; }

# --- Host side: unix socket -> Basalt ------------------------------------------
RUN_DIR=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/agent-jail.XXXXXX")
chmod 700 "$RUN_DIR"
SOCK="$RUN_DIR/basalt.sock"
socat UNIX-LISTEN:"$SOCK",fork,mode=600 TCP:"$BASALT_HOST":"$BASALT_PORT" 2>/dev/null &
BRIDGE_PID=$!
cleanup() { kill "$BRIDGE_PID" 2>/dev/null || true; rm -rf "$RUN_DIR"; }
trap cleanup EXIT INT TERM
for _ in $(seq 50); do [[ -S "$SOCK" ]] && break; sleep 0.05; done
[[ -S "$SOCK" ]] || { echo "agent-jail: Basalt bridge failed to start" >&2; exit 4; }

# --- Build the jail ----------------------------------------------------------
ARGS=(
  --die-with-parent --new-session
  --unshare-all                    # new net namespace (loopback only), pid, ipc, uts, user, cgroup
  --hostname agent-jail
  --ro-bind /usr /usr
  --symlink usr/bin /bin --symlink usr/sbin /sbin
  --symlink usr/lib /lib --symlink usr/lib64 /lib64
  --ro-bind /etc /etc              # no secrets readable here without root; tools need it
  --proc /proc --dev /dev
  --tmpfs /tmp --tmpfs /run --tmpfs /var/tmp
  --tmpfs /home
  --dir "$JAIL_HOME"
  --bind "$PROJECT" "$PROJECT"
  --bind "$SOCK" /run/basalt.sock
  --chdir "$PROJECT"
  --clearenv
  --setenv HOME "$JAIL_HOME"
  --setenv USER agent --setenv LOGNAME agent
  --setenv PATH /usr/local/bin:/usr/bin:/bin
  --setenv LANG "${LANG:-C.UTF-8}"
  --setenv TERM "${TERM:-xterm-256color}"
  --setenv TMPDIR /tmp
  --setenv OPENAI_BASE_URL "http://127.0.0.1:${BASALT_PORT}/v1"
  --setenv OPENAI_API_KEY basalt
  --setenv GIT_TERMINAL_PROMPT 0
  --setenv GIT_AUTHOR_NAME "Agent Jobs" --setenv GIT_AUTHOR_EMAIL agent@localhost
  --setenv GIT_COMMITTER_NAME "Agent Jobs" --setenv GIT_COMMITTER_EMAIL agent@localhost
)
[[ -d /usr/local ]] && ARGS+=(--ro-bind /usr/local /usr/local)
for d in "${RO_DIRS[@]}"; do ARGS+=(--ro-bind "$d" "$d"); done
for d in "${RW_DIRS[@]}"; do ARGS+=(--bind "$d" "$d"); done
# Extra env passed through on purpose (e.g. ANDROID_HOME, JAVA_HOME, NODE path). Never secrets.
for v in ANDROID_HOME ANDROID_SDK_ROOT JAVA_HOME GRADLE_USER_HOME NODE_PATH JAIL_EXTRA_PATH; do
  [[ -n "${!v:-}" ]] && ARGS+=(--setenv "$v" "${!v}")
done
[[ -n "${JAIL_EXTRA_PATH:-}" ]] && ARGS+=(--setenv PATH "$JAIL_EXTRA_PATH:/usr/local/bin:/usr/bin:/bin")

# Not exec: the EXIT trap must stop the bridge afterwards.
# Inside the jail: listen on 127.0.0.1:8080 and forward to the socket, then run the command.
set +e
bwrap "${ARGS[@]}" -- /bin/bash -c '
  socat TCP-LISTEN:'"$BASALT_PORT"',bind=127.0.0.1,fork,reuseaddr UNIX-CONNECT:/run/basalt.sock &
  for i in $(seq 50); do (exec 3<>/dev/tcp/127.0.0.1/'"$BASALT_PORT"') 2>/dev/null && break; sleep 0.05; done
  exec "$@"
' agent-jail "$@"
exit $?
