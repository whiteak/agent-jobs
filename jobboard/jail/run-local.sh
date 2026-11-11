#!/usr/bin/env bash
# JAIL: run-local.sh MODE HOSTDIR NAME CONTAINER -- AGENT_ARGS...
# Runs ONE local agent (local-claude / local-qwen), or a build command (sandbox-run),
# non-interactively in the offline
# local-job container (same flags as normal local jobs: jail/podman-local.sh).
# Called only by agent-broker.py, which has already validated every argument.
set -uo pipefail
HERE=$(dirname "$(realpath "$0")")
. "$HERE/podman-local.sh"
mode=$1 hostdir=$2 name=$3 cname=$4; shift 4; [ "${1:-}" = -- ] && shift
case "$mode" in local-claude|local-qwen) entry=("$mode") ;; sandbox-run) entry=(bash) ;; *) echo "run-local: bad mode" >&2; exit 2 ;; esac
jail_bridge_start || exit 1
trap jail_bridge_stop EXIT
trap 'podman stop -t 5 "$cname" >/dev/null 2>&1; exit 143' INT TERM
jail_local_args "$hostdir" "$name"
podman run --name "$cname" "${JAIL_ARGS[@]}" "${IMAGE:-localhost/local-agent:latest}" "${entry[@]}" "$@" </dev/null &
wait $!
