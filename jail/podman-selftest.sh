#!/usr/bin/env bash
# JAIL: podman-selftest.sh - prove a LOCAL-AI job container has no internet and no secrets.
# Uses jail_local_args from podman-local.sh, the same function ~/agent-sandbox/agent uses
# for local-claude / local-qwen jobs, so the test and real jobs cannot drift apart.
# The job board runs this before local jobs (result cached 10 min) and refuses to start
# a job if any line FAILs. Exit 0 = all OK. Written by Claude, 2026-11-11.
set -uo pipefail
HERE=$(dirname "$(realpath "$0")")
. "$HERE/podman-local.sh"
IMAGE=${IMAGE:-localhost/local-agent:latest}
NAME=selftest-project
PROJ=$(mktemp -d "$JAIL_STATE/selftest.XXXXXX")
cleanup() { jail_bridge_stop; rm -rf "$PROJ"; }
trap cleanup EXIT INT TERM

if ! curl -sf -m 3 http://127.0.0.1:8080/v1/models >/dev/null; then
  echo "FAIL  Basalt is not answering on the host (127.0.0.1:8080)"; echo "SANDBOX SELF-TEST FAILED"; exit 1
fi
jail_bridge_start || { echo "FAIL  Basalt bridge did not start"; echo "SANDBOX SELF-TEST FAILED"; exit 1; }
jail_local_args "$PROJ" "$NAME"

read -r -d '' CHECKS <<'SH'
ok()   { echo "OK    $1"; }
bad()  { echo "FAIL  $1"; }
want_open()  { if timeout 5 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null; then ok "$1"; else bad "$1"; fi; }
want_block() { if timeout 5 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null; then bad "$1 (connected!)"; else ok "$1"; fi; }
if timeout 10 bash -c 'exec 3<>/dev/tcp/127.0.0.1/8080 && printf "GET /v1/models HTTP/1.0\r\n\r\n" >&3 && head -c 64 <&3 | grep -q "HTTP/1.[01] 200"'; then
  ok "Basalt API reachable on 127.0.0.1:8080"; else bad "Basalt API reachable on 127.0.0.1:8080"; fi
want_block "Internet by IP blocked (1.1.1.1:443)" 1.1.1.1 443
if timeout 5 getent hosts example.com >/dev/null 2>&1; then bad "DNS blocked (example.com resolved!)"; else ok "DNS blocked (example.com)"; fi
want_block "LAN blocked (192.168.1.1:80)" 192.168.1.1 80
want_block "Job board blocked (127.0.0.1:8090)" 127.0.0.1 8090
ifs=$(tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ' | tr '\n' ' ')
[ "$ifs" = "lo " ] && ok "Only loopback interface" || bad "Only loopback interface (found: $ifs)"
found=$(find /home/agent -xdev \( -name .credentials.json -o -name hosts.yml -o -name .git-credentials -o -name .netrc -o -name id_rsa -o -name id_ed25519 \) -print -quit 2>/dev/null)
[ -z "$found" ] && ok "No credential files in /home/agent" || bad "No credential files in /home/agent (found $found)"
[ ! -e /home/agent/.config/gh ] && ok "No GitHub CLI login (~/.config/gh)" || bad "No GitHub CLI login (~/.config/gh exists)"
[ -z "$(git config --global --get-regexp '^credential' 2>/dev/null)" ] && ok "No git credential helper" || bad "No git credential helper"
leak=$(env | grep -Ei 'token|secret|github|anthropic_api|password|ssh_auth|api_key' | grep -v '^ANTHROPIC_AUTH_TOKEN=basalt-local$' | cut -d= -f1 | tr '\n' ' ')
[ -z "$leak" ] && ok "No secrets in environment" || bad "No secrets in environment (found: $leak)"
others=$(ls -A /work | grep -vx -e "$JAIL_NAME" -e .agent-logs | tr '\n' ' ')
[ -z "$others" ] && [ -d "/work/$JAIL_NAME" ] && ok "Only the job's own project under /work" || bad "Only the job's own project under /work (also: $others)"
[ -z "$(ls -A /work/.agent-logs 2>/dev/null)" ] || bad "/work/.agent-logs is not empty"
[ ! -e /home/jason ] && ok "Real home not visible" || bad "Real home not visible (/home/jason exists)"
touch "/work/$JAIL_NAME/.w" 2>/dev/null && rm "/work/$JAIL_NAME/.w" && ok "Project folder writable" || bad "Project folder writable"
{ touch /usr/x || touch /etc/x || touch /opt/agent/x; } 2>/dev/null && bad "Cannot write system folders" || ok "Cannot write system folders"
SH

out=$(podman run "${JAIL_ARGS[@]}" -e JAIL_NAME="$NAME" "$IMAGE" bash -c "$CHECKS" 2>&1)
rc=$?
echo "$out" | grep -E '^(OK|FAIL) '
fails=$(echo "$out" | grep -c '^FAIL ')
oks=$(echo "$out" | grep -c '^OK ')
if [ $rc -ne 0 ] && [ "$oks" -eq 0 ]; then echo "FAIL  container did not run: $(echo "$out" | tail -3 | tr '\n' ' ')"; fails=1; fi
[ "$oks" -lt 14 ] && [ "$fails" -eq 0 ] && { echo "FAIL  only $oks checks ran (expected 14)"; fails=1; }
if [ "$fails" -eq 0 ]; then echo "SANDBOX SELF-TEST PASSED"; exit 0; else echo "SANDBOX SELF-TEST FAILED"; exit 1; fi
