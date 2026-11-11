# JAIL: podman-local.sh - the ONE place that defines how a local-AI job container runs.
# Sourced by ~/agent-sandbox/agent (local-claude, local-qwen) and by podman-selftest.sh,
# so the self-test checks exactly the flags real jobs use.
#
#   jail_bridge_start            host side: private unix socket -> Basalt 127.0.0.1:8080
#   jail_local_args HOSTDIR NAME fills JAIL_ARGS with the podman run flags (no image/cmd)
#   jail_bridge_stop             stops the host bridge, removes the socket dir
#
# What the container gets:
#   * no network at all (--network none); Basalt only through the unix socket bridge
#   * /home/agent = state/home-local (caches + agent config, NO credentials)
#   * /work/NAME  = only this job's project folder (rest of /work is an empty tmpfs)
#   * /opt/android-sdk (shared SDK), no capabilities, no new privileges
# Fallback: AGENT_LOCAL_NET=pasta uses the older pasta --splice-only offline mode
# (also blocks internet/LAN/DNS) instead of --network none + bridge.
# Written by Claude, 2026-11-11.

JAIL_DIR="${JAIL_DIR:-$HOME/agent-sandbox/jail}"
JAIL_STATE="${JAIL_STATE:-$HOME/agent-sandbox/state}"
JAIL_HOME_LOCAL="${JAIL_HOME_LOCAL:-$JAIL_STATE/home-local}"
JAIL_RUNDIR="${JAIL_RUNDIR:-}"; JAIL_BRIDGE_PID="${JAIL_BRIDGE_PID:-}"

jail_bridge_start() {
  [ "${AGENT_LOCAL_NET:-none}" = pasta ] && return 0
  command -v socat >/dev/null || { echo "agent: socat is not installed (needed for the offline Basalt bridge)" >&2; return 1; }
  JAIL_RUNDIR=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/agent-local.XXXXXX") || return 1
  chmod 700 "$JAIL_RUNDIR"
  socat UNIX-LISTEN:"$JAIL_RUNDIR/basalt.sock",fork,mode=600 TCP:127.0.0.1:8080 2>/dev/null &
  JAIL_BRIDGE_PID=$!
  local i; for i in $(seq 100); do [ -S "$JAIL_RUNDIR/basalt.sock" ] && return 0; sleep 0.05; done
  echo "agent: the Basalt bridge did not start" >&2; return 1
}

jail_bridge_stop() {
  [ -n "$JAIL_BRIDGE_PID" ] && kill "$JAIL_BRIDGE_PID" 2>/dev/null
  [ -n "$JAIL_RUNDIR" ] && rm -rf "$JAIL_RUNDIR"
  JAIL_BRIDGE_PID=""; JAIL_RUNDIR=""
}

jail_local_args() {  # HOSTDIR NAME
  local hostdir=$1 name=$2
  JAIL_ARGS=(--rm --init --userns=keep-id --cap-drop=ALL --security-opt no-new-privileges
             --hostname agent-sandbox
             --tmpfs /work:rw,mode=1777 -v "$hostdir:/work/$name" -w "/work/$name"
             -e HOME=/home/agent -e TERM="${TERM:-xterm-256color}"
             -v "$JAIL_HOME_LOCAL:/home/agent" -v "$JAIL_STATE/android-sdk:/opt/android-sdk")
  if [ "${AGENT_LOCAL_NET:-none}" = pasta ]; then
    JAIL_ARGS+=(--network "pasta:--splice-only,-T,8080")
  else
    JAIL_ARGS+=(--network none -v "$JAIL_RUNDIR:/run/basalt"
                -v "$JAIL_DIR/bridge.py:/opt/jail/bridge.py:ro" -v "$JAIL_DIR/entry.sh:/opt/jail/entry.sh:ro"
                --entrypoint /opt/jail/entry.sh)
  fi
}
