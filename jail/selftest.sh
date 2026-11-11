#!/usr/bin/env bash
# selftest.sh - prove the jail blocks the internet but still reaches Basalt.
# Agent Jobs runs this before every job and refuses to start if any line FAILs.
# Usage: selftest.sh [path/to/agent-jail.sh]
set -uo pipefail
JAIL=${1:-$(dirname "$(realpath "$0")")/agent-jail.sh}
PROJ=$(mktemp -d "${TMPDIR:-/tmp}/jail-selftest.XXXXXX")
trap 'rm -rf "$PROJ"' EXIT
fail=0
check() { # name, expected (pass|block), command...
  local name=$1 want=$2; shift 2
  if "$JAIL" --project "$PROJ" -- bash -c "$*" >/dev/null 2>&1; then got=pass; else got=block; fi
  if [[ $got == "$want" ]]; then echo "OK    $name"; else echo "FAIL  $name (got $got, wanted $want)"; fail=1; fi
}
check "Basalt API reachable"           pass  "exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /v1/models HTTP/1.0\r\n\r\n' >&3 && head -c 12 <&3 | grep -q HTTP"
check "Internet by IP blocked"         block "timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443'"
check "Internet by name blocked"       block "timeout 5 getent hosts example.com"
check "LAN (phone/router) blocked"     block "timeout 5 bash -c 'exec 3<>/dev/tcp/192.168.1.1/80'"
check "Job board 8090 not reachable"   block "timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/8090'"
check "Only loopback interface"        pass  "[ \"\$(tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d ' ')\" = lo ]"
check "Project folder writable"        pass  "touch '$PROJ/x' && rm '$PROJ/x'"
check "Real home not visible"          block "ls '$HOME/.ssh' || ls '$HOME/.mozilla' || ls '$HOME/snap' || ls '$HOME/.config'"
check "Basalt config not visible"      block "cat '$HOME/basalt_config.json'"
check "No secrets in environment"      block "env | grep -Eiq 'token|secret|anthropic|github|ssh_auth|password'"
check "Cannot write outside project"   block "touch /usr/x || touch /etc/x"
if [[ $fail == 0 ]]; then echo "SANDBOX SELF-TEST PASSED"; else echo "SANDBOX SELF-TEST FAILED"; fi
exit $fail
