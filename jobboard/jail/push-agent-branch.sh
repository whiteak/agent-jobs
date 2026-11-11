#!/usr/bin/env bash
# push-agent-branch.sh - push a finished job's branch WITHOUT trusting the job's repo.
#
# Runs on the host, outside any agent container, after the job ends. The job's .git folder
# was writable by the agent, so its hooks and config (core.hooksPath, core.fsmonitor,
# credential.helper, url.insteadOf, sshCommand...) could run commands or redirect the push.
# So no git command touches it outside a jail:
#   1. bwrap jail (no network, no creds, sees only the project + an output dir):
#        git bundle create  -> a plain data file
#   2. host: fetch that bundle (fsck'd, hooks off) into a harness-owned clean mirror
#        state/push-mirrors/<hash>.git
#   3. throwaway podman container that mounts ONLY the mirror (read-only) and, for GitHub,
#        state/push-creds (read-only): git push REMOTE agent/...:agent/...
#
# Usage: push-agent-branch.sh JOB_REPO BRANCH REMOTE
#   JOB_REPO  the job's work folder (the agent's copy)
#   BRANCH    must be agent/<something>
#   REMOTE    recorded by the job board at job START (never read from the repo afterwards):
#             https://github.com/OWNER/REPO(.git)   (git@github.com:OWNER/REPO is converted)
#             or an absolute path to a local bare repository (testing, local backups)
# Status: 2026-11-11 (Claude), tested on Ubuntu 26.04.1.
set -euo pipefail
JOB_REPO=$(realpath "$1"); BRANCH=$2; REMOTE=$3
HERE=$(dirname "$(realpath "$0")")
JAIL=$HERE/agent-jail.sh
STATE=${AGENT_STATE:-$HOME/agent-sandbox/state}
MIRRORS=${AGENT_MIRRORS:-$STATE/push-mirrors}
CREDS=${AGENT_PUSH_CREDS:-$STATE/push-creds}
IMAGE=${IMAGE:-localhost/local-agent:latest}

[[ "$BRANCH" =~ ^agent/[A-Za-z0-9._/-]+$ && "$BRANCH" != *..* && "$BRANCH" != */ && "$BRANCH" != *.lock ]] \
  || { echo "refusing branch '$BRANCH' (only agent/... branches are pushed)" >&2; exit 2; }

# --- which remote, and how -------------------------------------------------------
if [[ "$REMOTE" =~ ^git@github\.com:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$ ]]; then
  REMOTE="https://github.com/${BASH_REMATCH[1]}/${BASH_REMATCH[2]}"
fi
if [[ "$REMOTE" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  kind=github
  [[ -f "$CREDS/gh/hosts.yml" ]] || { echo "no GitHub login in $CREDS (run: ~/agent-sandbox/agent github-login)" >&2; exit 3; }
elif [[ "$REMOTE" == /* ]]; then
  kind=local
  REMOTE=$(realpath "$REMOTE")
  [[ -f "$REMOTE/HEAD" && -d "$REMOTE/objects" && -d "$REMOTE/refs" && ! -e "$REMOTE/.git" ]] \
    || { echo "refusing remote '$REMOTE' (not a bare git repository)" >&2; exit 2; }
  case "$REMOTE/" in "$JOB_REPO/"*) echo "refusing remote inside the job's own folder" >&2; exit 2 ;; esac
else
  echo "refusing remote '$REMOTE' (only https://github.com/OWNER/REPO or a local bare repo path)" >&2; exit 2
fi

# --- 1. bundle inside the jail ---------------------------------------------------
OUT=$(mktemp -d "$STATE/push-tmp.XXXXXX"); trap 'rm -rf "$OUT"' EXIT
"$JAIL" --project "$JOB_REPO" --rw "$OUT" -- \
  git -c core.hooksPath=/dev/null -c core.fsmonitor=false bundle create "$OUT/push.bundle" "refs/heads/$BRANCH" \
  >"$OUT/bundle.log" 2>&1 || { echo "could not bundle $BRANCH:"; tail -5 "$OUT/bundle.log"; exit 4; } >&2

# --- 2. clean mirror on the host -------------------------------------------------
name=$(printf '%s' "$REMOTE" | sha1sum | cut -c1-12)
M="$MIRRORS/$name.git"
[[ -d "$M" ]] || { mkdir -p "$MIRRORS"; git init --bare -q "$M"; }
safe=(-c core.hooksPath=/dev/null -c transfer.fsckObjects=true -c fetch.fsckObjects=true)
git "${safe[@]}" --git-dir="$M" bundle verify -q "$OUT/push.bundle" >/dev/null
git "${safe[@]}" --git-dir="$M" fetch -q "$OUT/push.bundle" "+refs/heads/$BRANCH:refs/heads/$BRANCH"
sha=$(git --git-dir="$M" rev-parse "refs/heads/$BRANCH")

# --- 3. push from a throwaway container ------------------------------------------
run=(podman run --rm --userns=keep-id --cap-drop=ALL --security-opt no-new-privileges
     -e HOME=/tmp -v "$M:/mirror:ro" --entrypoint git)
if [[ $kind == github ]]; then
  run+=(--network pasta -v "$CREDS:/creds:ro" -e GH_CONFIG_DIR=/creds/gh -e GIT_CONFIG_GLOBAL=/creds/gitconfig
        -e GIT_TERMINAL_PROMPT=0)
  "${run[@]}" "$IMAGE" -c core.hooksPath=/dev/null --git-dir=/mirror \
    push "$REMOTE" "refs/heads/$BRANCH:refs/heads/$BRANCH"
else
  # local bare repo: no network, no creds; its own hooks are switched off too
  run+=(--network none -e GIT_CONFIG_GLOBAL=/dev/null -v "$REMOTE:/remote")
  "${run[@]}" "$IMAGE" -c core.hooksPath=/dev/null -c safe.directory='*' --git-dir=/mirror \
    push --receive-pack="git -c core.hooksPath=/dev/null -c safe.directory=* receive-pack" \
    /remote "refs/heads/$BRANCH:refs/heads/$BRANCH"
fi
echo "pushed $BRANCH ($sha) to $REMOTE"
