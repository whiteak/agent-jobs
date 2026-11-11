#!/usr/bin/env bash
# JAIL: fetch-deps.sh FOLDER - download a project's packages so OFFLINE agents can build it.
#
# The AI never gets internet; the harness downloads. This runs in a throwaway podman
# container WITH internet but:
#   * no model, no Basalt route, no credentials (fresh empty home)
#   * only ~/agent-work/FOLDER, plus the shared caches the offline agents use:
#     state/home-local/.gradle, state/home-local/.npm, state/android-sdk (read-write)
#   * fixed commands only, 15 minute limit, started only when Jason presses the button
# Caveat: Gradle must EVALUATE build.gradle(.kts) to know the dependencies, and those files
# may be agent-written - so agent code can run here while online. It has nothing to steal
# except this project folder and the caches, and no logins to use.
#
#   Gradle (gradlew):          compileSdk/buildTools platforms via sdkmanager (only those named
#                              in the build files), then ./gradlew --no-daemon jailResolveAll
#                              (harness init script resolve-deps.gradle) + assembleDebug --dry-run
#   npm (package-lock.json):   npm ci --ignore-scripts        (package.json only: npm install --ignore-scripts)
#   pip (requirements.txt):    pip download -r requirements.txt -d .agent-cache/pip
# Written by Claude, 2026-11-11.
set -uo pipefail
FOLDER=${1:?usage: fetch-deps.sh FOLDER}
[[ "$FOLDER" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || { echo "bad folder name" >&2; exit 2; }
HERE=$(dirname "$(realpath "$0")")
WORK=${AGENT_WORK:-$HOME/agent-work}
STATE=${AGENT_STATE:-$HOME/agent-sandbox/state}
HL=$STATE/home-local
IMAGE=${IMAGE:-localhost/local-agent:latest}
LIMIT=${FETCH_LIMIT:-900}
DIR=$WORK/$FOLDER
[ -d "$DIR" ] || { echo "no such folder: $DIR" >&2; exit 2; }
mkdir -p "$HL/.gradle" "$HL/.npm"
CNAME="agentfetch-$FOLDER-$$"

read -r -d '' SCRIPT <<'SH'
set -o pipefail
did=0; rc=0
step() { echo; echo "=== $*"; }
if [ -x ./gradlew ] || [ -f ./gradlew ]; then
  did=1
  # SDK platforms / build-tools named in the build files (only those, nothing else)
  apis=$(grep -rhoE 'compileSdk(Version)?[ =(]*[0-9]{2}' --include='*.gradle' --include='*.gradle.kts' . 2>/dev/null | grep -oE '[0-9]{2}$' | sort -u)
  bts=$(grep -rhoE 'buildToolsVersion[ =(]*"[0-9.]+"' --include='*.gradle' --include='*.gradle.kts' . 2>/dev/null | grep -oE '[0-9.]+' | sort -u)
  pk=()
  for a in $apis; do [ -d "/opt/android-sdk/platforms/android-$a" ] || pk+=("platforms;android-$a"); done
  for b in $bts;  do [ -d "/opt/android-sdk/build-tools/$b" ] || pk+=("build-tools;$b"); done
  if [ ${#pk[@]} -gt 0 ]; then
    step "sdkmanager ${pk[*]}"
    yes | sdkmanager --sdk_root=/opt/android-sdk --licenses >/dev/null 2>&1
    sdkmanager --sdk_root=/opt/android-sdk "${pk[@]}" | sed '/^\[/d' || rc=1
  else
    step "Android SDK: platforms named in the build files are already installed (${apis:-none named})"
  fi
  step "./gradlew --no-daemon jailResolveAll (download every dependency)"
  ./gradlew --no-daemon --console=plain -I /opt/jail/resolve-deps.gradle jailResolveAll 2>&1 | sed -E '/^(> Task|$)/d' | tail -40 || rc=1
  step "./gradlew --no-daemon assembleDebug --dry-run (plugins and task graph)"
  ./gradlew --no-daemon --console=plain assembleDebug --dry-run 2>&1 | tail -5 || echo "(no assembleDebug task - fine for non-Android projects)"
fi
if [ -f package-lock.json ]; then
  did=1; step "npm ci --ignore-scripts"
  npm ci --ignore-scripts --no-audit --no-fund 2>&1 | tail -20 || rc=1
elif [ -f package.json ]; then
  did=1; step "npm install --ignore-scripts (no package-lock.json yet; this creates one)"
  npm install --ignore-scripts --no-audit --no-fund 2>&1 | tail -20 || rc=1
fi
if [ -f requirements.txt ]; then
  did=1; step "pip download -r requirements.txt -d .agent-cache/pip"
  mkdir -p .agent-cache/pip
  pip download -q -r requirements.txt -d .agent-cache/pip 2>&1 | tail -20 || rc=1
  echo "Offline install: pip install --no-index --find-links .agent-cache/pip -r requirements.txt"
fi
[ $did = 1 ] || { echo "Nothing to download: no gradlew, package.json or requirements.txt in this folder."; }
exit $rc
SH

echo "Download packages for ~/agent-work/$FOLDER  ($(date '+%Y-%m-%d %H:%M'))"
echo "Sandbox: internet on, no logins, no model, only this folder + shared caches, limit ${LIMIT}s"
start=$(date +%s)
timeout --signal=TERM "$LIMIT" podman run --rm --name "$CNAME" --init --userns=keep-id --cap-drop=ALL \
  --security-opt no-new-privileges --network pasta --hostname agent-fetch \
  --tmpfs /home/agent:rw,mode=1777 -e HOME=/home/agent -e GRADLE_USER_HOME=/home/agent/.gradle \
  -v "$HL/.gradle:/home/agent/.gradle" -v "$HL/.npm:/home/agent/.npm" \
  -v "$STATE/android-sdk:/opt/android-sdk" -v "$HERE/resolve-deps.gradle:/opt/jail/resolve-deps.gradle:ro" \
  -v "$DIR:/work/$FOLDER" -w "/work/$FOLDER" --entrypoint bash "$IMAGE" -c "$SCRIPT"
rc=$?
if [ $rc = 124 ]; then podman stop -t 5 "$CNAME" >/dev/null 2>&1; echo; echo "STOPPED: took longer than ${LIMIT}s"; fi
echo; echo "Finished in $(( $(date +%s) - start ))s - $([ $rc = 0 ] && echo DOWNLOAD OK || echo "DOWNLOAD FAILED (exit $rc)")"
exit $rc
