#!/usr/bin/env python3
"""Upgrade 7b: the Changes tab - what a job changed, file by file.

    summary(m, git, ref)     the file list with +/- counts for one view (+ the view picker)
    file_diff(m, git, ref, path)   one file's diff, parsed into rows (unified and side by side)

A view (ref) is one of:

    all      everything the job changed since its base commit, uncommitted included
    c:<sha>  only that commit's changes (a commit dated inside the job window)
    k:<n>    everything up to checkpoint n since the base commit

Every git command runs in the agent's own jail (upgrades.make_wrap -> jail/run-local.sh
sandbox-run) with safegit's flags, because a job repo is not trusted: the agent could
have planted hooks, an fsmonitor command or a pager in .git/config. Refs coming from the
browser are matched against the job's own commits/checkpoints before they reach the
command line, and a file path only ever crosses into the script base64'd (the same trick
reviewbundle uses for untracked names).

Binary and generated files are listed but never shown, using the review bundle's rules
(reviewbundle._skip) plus diffparse.is_generated. Calls are cached briefly, so a running
job that refreshes every 10 s does not start a container on every poll.

Standard library only. The diff parser is agentjobs_upgrades.diffparse (not rewritten here).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import reviewbundle as rb
from agentjobs_upgrades import diffparse
from agentjobs_upgrades.safegit import SAFE_FLAGS

REF_ALL = "all"
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
EMPTY_TREE = rb.EMPTY_TREE

LIST_MAX = 3000            # files per view
UNTRACKED_MAX = 1500       # untracked files listed
UNTRACKED_READ = 2_000_000  # bigger untracked files are listed, not measured
DIFF_MAX_BYTES = 400_000   # one file's diff text before it is cut
DIFF_MAX_LINES = 12000     # hunk lines per file before the rest is dropped
SUMMARY_TTL = 8.0          # the page refreshes a running job every 10 s
REFS_TTL = 30.0
CTX_RUN = 6                # unchanged lines in a row before the middle collapses

_STATUS = {"A": "added", "D": "deleted", "M": "modified", "T": "modified",
           "R": "renamed", "C": "renamed", "U": "modified"}

# ---------------------------------------------------------------- the safe git shell

_GIT_FN = "g() { git " + " ".join(shlex.quote(f) for f in SAFE_FLAGS) + ' "$@"; }'
_PRELUDE = ("export GIT_CONFIG_NOSYSTEM=1 GIT_PAGER=cat GIT_TERMINAL_PROMPT=0 "
            "GIT_MERGE_AUTOEDIT=no\n" + _GIT_FN + "\n")

MARKS = ("@@CHK", "@@BR", "@@CM", "@@CK", "@@NS", "@@NU", "@@UT", "@@EN")

PROBE_SCRIPT = """
__PRELUDE__echo '@@CHK'; g rev-parse --is-inside-work-tree 2>/dev/null
echo '@@BR'; g rev-parse --abbrev-ref HEAD 2>/dev/null
echo '@@CM'; g log --format='%H%x09%ct%x09%s' --since=__LO__ --until=__HI__ HEAD 2>/dev/null | head -n 80
echo '@@CK'; __CKS__
echo '@@EN'
"""

LIST_SCRIPT = """
__PRELUDE____RNG__
echo '@@NS'; g diff --name-status -M $RNG 2>/dev/null | head -n __MAXL__
echo '@@NU'; g diff --numstat -M $RNG 2>/dev/null | head -n __MAXL__
echo '@@UT'; __UT__
echo '@@EN'
"""

UNTRACKED_BLOCK = """i=0
g ls-files --others --exclude-standard -z 2>/dev/null | {
  while IFS= read -r -d '' f; do
    i=$((i+1))
    [ "$i" -le __MAXU__ ] || continue
    s=$(wc -c < "$f" 2>/dev/null | tr -d ' '); s=${s:--1}
    l=-1; x=2
    if [ "$s" != "-1" ] && [ "$s" -lt __READ__ ]; then
      l=$(wc -l < "$f" 2>/dev/null | tr -d ' '); l=${l:--1}
      b=$(LC_ALL=C tr -d '\\000' < "$f" 2>/dev/null | wc -c | tr -d ' ')
      x=0; [ "$s" != "$b" ] && x=1
    fi
    printf '@@U %s %s %s %s\\n' "$(printf %s "$f" | base64 | tr -d '\\n')" "$l" "$s" "$x"
  done
  printf '@@C %s\\n' "$i"
}"""

FILE_SCRIPT = """
__PRELUDE__P=$(printf %s '__B64__' | base64 -d)
Q=$(printf %s '__B64B__' | base64 -d)
case "$P" in ''|/*|../*|*/..|*/../*|.git|.git/*) exit 0 ;; esac
if [ -n "$Q" ]; then case "$Q" in /*|../*|*/..|*/../*|.git|.git/*) Q= ;; esac; fi
__RNG____DIFF__
"""

DIFF_TRACKED = 'g diff -M --no-color -U3 $RNG -- "$P"${Q:+ "$Q"} 2>/dev/null | head -c __MAXB__\n'
DIFF_UNTRACKED = 'g diff --no-index --no-color -U3 -- /dev/null "$P" 2>/dev/null | head -c __MAXB__\n'


def git_for(m, work):
    """SafeGit for the job's project folder (the job's jail; host shell in JOBBOARD_JAIL=host tests)."""
    import upgrades
    return upgrades.git_for(m, Path(work) / m["folder"])


def sh(git, script):
    """Run a shell script in the job's jail with the project folder as cwd. Never raises."""
    argv = git.wrap(git.repo, script) if git.wrap is not None else ["bash", "-c", script]
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_PAGER="cat", GIT_TERMINAL_PROMPT="0")
    try:
        p = subprocess.run(argv, cwd=str(git.repo), capture_output=True, text=True,
                           timeout=git.timeout, env=env, stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError) as ex:
        return f"(could not read: {ex})"
    return p.stdout


def _segments(out):
    """stdout -> {CHK: text, BR: text, ...}. Markers are whole lines, and no repo output
    line can look like one (numstat/name-status lines always carry a leading count/state)."""
    seg, key = {}, None
    for ln in out.splitlines():
        st = ln.strip()
        if st in MARKS:
            key = st[2:]
            seg.setdefault(key, "")
            continue
        if key is not None:
            seg[key] += ln + "\n"
    return seg


# ---------------------------------------------------------------- views the page can ask for

def _window(m):
    started = m.get("started") or m.get("created")
    return (int(started - 5) if started else 0), int((m.get("finished") or time.time()) + 120)


def _checkpoints(m):
    out = []
    for c in (m.get("checkpoints") or {}).get("checkpoints", []):
        sha = str(c.get("commit") or "")
        if SHA_RE.fullmatch(sha):
            try:
                n = int(c.get("n"))
            except (TypeError, ValueError):
                continue
            out.append({"n": n, "commit": sha, "step": str(c.get("step") or "")[:80]})
    return out


def _base(m):
    """The base commit recorded when the job started ('' when the record has none)."""
    gs = str(m.get("git_start") or "")
    return gs if SHA_RE.fullmatch(gs) else ""


def probe(m, git):
    """Branch, the job's own commits, and which checkpoint commits still exist."""
    lo, hi = _window(m)
    cps = _checkpoints(m)
    cks = []
    for c in cps[:40]:
        cks.append(f'printf \'@@K {c["n"]} \'; g rev-parse --verify -q \'{c["commit"]}\' '
                   f'>/dev/null 2>&1 && echo \'ok {c["commit"]}\' || echo \'gone {c["commit"]}\'')
    script = (PROBE_SCRIPT.replace("__PRELUDE__", _PRELUDE).replace("__LO__", str(lo))
              .replace("__HI__", str(hi)).replace("__CKS__", "; ".join(cks) or "true"))
    out = sh(git, script)
    if out.startswith("(could not read"):
        return {"error": out.strip("() ") or "could not read the project"}
    seg = _segments(out)
    if "true" not in seg.get("CHK", ""):
        return {"error": "not a git repository"}
    commits = []
    for ln in seg.get("CM", "").splitlines():
        p = ln.split("\t")
        if len(p) >= 3 and SHA_RE.fullmatch(p[0]):
            commits.append({"sha": p[0], "time": int(p[1]) if p[1].isdigit() else 0,
                            "subject": "\t".join(p[2:])[:120]})
    alive = {}
    for ln in seg.get("CK", "").splitlines():
        p = ln[3:].split() if ln.startswith("@@K ") else []
        if len(p) >= 2 and p[0].isdigit():
            alive[int(p[0])] = p[1]
    refs = [{"ref": REF_ALL, "label": "Everything the job changed", "kind": "all"}]
    for c in commits:
        refs.append({"ref": "c:" + c["sha"],
                     "label": c["sha"][:8] + " " + (c["subject"] or "(no message)"), "kind": "commit"})
    for c in cps:
        if alive.get(c["n"]) == "gone":
            continue
        refs.append({"ref": "k:%d" % c["n"], "kind": "checkpoint",
                     "label": "Checkpoint %d %s" % (c["n"], c["step"] or "(no step text)")})
    return {"branch": seg.get("BR", "").strip()[:80], "commits": commits,
            "checkpoints": cps, "refs": refs}


def _rng(m, ref, pf):
    """Shell lines that set RNG to the validated range for this view ('' = bad ref)."""
    base = _base(m)
    if ref in ("", None, REF_ALL):
        if base:
            return f"RNG='{base}'\n"
        lo, hi = _window(m)
        return ('oldest=$(g log --format=%H --since=' + str(lo) + " --until=" + str(hi) +
                ' HEAD 2>/dev/null | tail -n 1); '
                'b=""; [ -n "$oldest" ] && b=$(g rev-parse --verify -q "$oldest^" 2>/dev/null); '
                'RNG="${b:-' + EMPTY_TREE + '}"\n')
    kind, _, val = str(ref).partition(":")
    if kind == "c" and any(val == c["sha"] for c in pf["commits"]):
        return (f'b=$(g rev-parse --verify -q "{val}^" 2>/dev/null); '
                f'RNG="${{b:-{EMPTY_TREE}}} {val}"\n')
    if kind == "k":
        cp = next((c for c in pf["checkpoints"] if str(c["n"]) == val), None)
        if cp:
            if base:
                return f'RNG=\'{base} {cp["commit"]}\'\n'
            return (f'b=$(g rev-parse --verify -q "{cp["commit"]}^" 2>/dev/null); '
                    f'RNG="${{b:-{EMPTY_TREE}}} {cp["commit"]}"\n')
    return ""


def _how(m, ref, pf):
    """One short line saying what the shown range is."""
    base = _base(m)
    if ref in ("", None, REF_ALL):
        if base:
            return "everything since " + base[:8] + " (where the job started), uncommitted included"
        return "the job's commits and any uncommitted changes"
    kind, _, val = str(ref).partition(":")
    if kind == "c":
        c = next((x for x in pf["commits"] if x["sha"] == val), None)
        return "commit " + val[:8] + (" " + c["subject"] if c and c["subject"] else "")
    cp = next((c for c in pf["checkpoints"] if str(c["n"]) == val), None)
    if cp:
        return ("everything up to checkpoint %s (%s)" % (cp["n"], cp["step"])) if cp["step"] else \
               "everything up to checkpoint %s" % cp["n"]
    return ""


# ---------------------------------------------------------------- the file list

def _unquote(p):
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        return p[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return p


def _name_status(text):
    out = []
    for ln in text.splitlines():
        f = ln.split("\t")
        if len(f) < 2:
            continue
        st = _STATUS.get(f[0][:1], "modified")
        if st == "renamed" and len(f) >= 3:
            out.append({"status": st, "old": _unquote(f[1]), "path": _unquote(f[2])})
        else:
            out.append({"status": st, "path": _unquote(f[1]), "old": _unquote(f[1])})
    return out


def _numstat(text):
    """path -> (added, removed, binary). Counts come from reviewbundle._parse_numstat
    (it also untangles 'a/{old => new}.py' rename paths); a '-' count means git says
    the file is binary."""
    counts = rb._parse_numstat(text)
    binary = set()
    for ln in text.splitlines():
        p = ln.split("\t")
        if len(p) >= 3 and (p[0] == "-" or p[1] == "-"):
            binary.update(rb._parse_numstat(ln))
    return {path: (a, d, path in binary) for path, (a, d) in counts.items()}


def _b64d(s):
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return None


def _untracked(text):
    """'@@U <b64 path> <lines> <bytes> <binflag>' lines -> untracked file entries.
    binflag: 0 text, 1 binary, 2 too big to measure here."""
    out, more = [], 0
    for ln in text.splitlines():
        if ln.startswith("@@C "):
            try:
                more = max(0, int(ln[4:].strip()) - UNTRACKED_MAX)
            except ValueError:
                pass
            continue
        if not ln.startswith("@@U "):
            continue
        p = ln[4:].split()
        if len(p) < 4:
            continue
        path = _b64d(p[0])
        if not path:
            continue

        def num(v):
            try:
                return int(v)
            except ValueError:
                return -1
        lines, size, flag = num(p[1]), num(p[2]), num(p[3])
        out.append({"path": path, "old": path, "status": "untracked",
                    "added": lines if lines > 0 else (1 if size > 0 else 0), "removed": 0,
                    "binary": flag == 1, "lines": lines, "size": size})
    return out, more


def _flags(entry):
    """Listed but not shown: binary, generated, or skipped by the review bundle's rules."""
    path = entry["path"]
    if entry.get("binary"):
        return False, "binary"
    if diffparse.is_generated(path):
        return False, "generated"
    if rb._skip(path):
        return False, "not reviewed"
    return True, ""


def _list(m, git, ref, pf):
    """The file list for one view: one jail call."""
    rng = _rng(m, ref, pf)
    if not rng:
        return {"error": "unknown revision"}
    ut = UNTRACKED_BLOCK.replace("__MAXU__", str(UNTRACKED_MAX)).replace("__READ__", str(UNTRACKED_READ)) \
        if ref in ("", None, REF_ALL) else "true"
    script = (LIST_SCRIPT.replace("__PRELUDE__", _PRELUDE).replace("__RNG__", rng)
              .replace("__MAXL__", str(LIST_MAX)).replace("__UT__", ut))
    out = sh(git, script)
    seg = _segments(out)
    files = []
    counts = _numstat(seg.get("NU", ""))
    for e in _name_status(seg.get("NS", "")):
        a, d, bn = counts.get(e["path"], (0, 0, False))
        e = dict(e, added=a, removed=d, binary=bn)
        files.append(e)
    untracked_more = 0
    if ut != "true":
        extra, untracked_more = _untracked(seg.get("UT", ""))
        for e in extra:
            counts.pop(e["path"], None)
            files.append(e)
        known = {e["path"] for e in files}
        for path, (a, d, bn) in counts.items():        # in numstat but not name-status
            if path not in known:
                files.append({"status": "modified", "path": path, "old": path,
                              "added": a, "removed": d, "binary": bn})
    for e in files:
        e["shown"], e["why"] = _flags(e)
    files.sort(key=lambda e: e["path"])
    return {"files": files[:LIST_MAX], "more": untracked_more + max(0, len(files) - LIST_MAX),
            "totals": {"files": len(files),
                       "added": sum(e["added"] for e in files),
                       "removed": sum(e["removed"] for e in files)}}


# ---------------------------------------------------------------- one file's diff

def bad_path(p):
    """Reject anything that could leave the project folder; git paths are repo data."""
    p = str(p or "")
    return (not p or p.startswith("/") or p.startswith(".git") or ".." in p.split("/")
            or "\x00" in p or len(p) > 1000)


def _hunks(hunks, limit):
    """Parsed hunks -> JSON rows (unified) + pair rows (side by side, from diffparse)."""
    rows, cut = [], False
    for h in hunks:
        lines = [["nl" if k == "\\" else k, o, n, t] for (k, o, n, t) in h.lines]
        pair = [[[l[0], l[1], l[2]] if l else None, [r[0], r[1], r[2]] if r else None]
                for l, r in diffparse.side_by_side(h)]
        rows.append({"o": h.old_start, "n": h.new_start, "header": h.header[:120],
                     "lines": lines, "pair": pair})
        limit -= len(lines)
        if limit <= 0:
            cut = True
            break
    return rows, cut


def file_diff(m, git, ref, path):
    """One file's diff as rows the page can render (unified and side by side)."""
    if bad_path(path):
        return {"error": "bad path"}
    pf = _cached(("probe", m.get("id")), REFS_TTL, lambda: probe(m, git))
    if pf.get("error"):
        return pf
    rng = _rng(m, ref, pf)
    if not rng:
        return {"error": "unknown revision"}
    # a rename needs both names in the pathspec, or git shows it as a plain new file
    ent = next((e for e in _cached(("list", m.get("id"), ref or REF_ALL), SUMMARY_TTL,
                                   lambda: _list(m, git, ref, pf)).get("files", [])
                if e.get("path") == path), None)
    old = ent.get("old", "") if ent and ent.get("old") != path else ""
    b64 = base64.b64encode(str(path).encode("utf-8")).decode("ascii")
    b64b = base64.b64encode(str(old).encode("utf-8")).decode("ascii") if old else ""
    script = (FILE_SCRIPT.replace("__PRELUDE__", _PRELUDE).replace("__B64__", b64)
              .replace("__B64B__", b64b))
    out = sh(git, script.replace("__RNG__", rng)
             .replace("__DIFF__", DIFF_TRACKED.replace("__MAXB__", str(DIFF_MAX_BYTES))))
    if not out.strip() and ref in ("", None, REF_ALL):
        out = sh(git, script.replace("__RNG__", _rng(m, REF_ALL, pf))
                 .replace("__DIFF__", DIFF_UNTRACKED.replace("__MAXB__", str(DIFF_MAX_BYTES))))
        if out.strip():
            ref = REF_ALL
    secs = [f for f in diffparse.parse(out) if f.path == path or f.old_path == path]
    if not secs:
        secs = diffparse.parse(out)
    if not secs:
        return {"error": "no diff for that file", "path": path}
    f = secs[0]
    shown, why = _flags({"path": f.path, "binary": f.binary})
    truncated = len(out) >= DIFF_MAX_BYTES
    hunks, cut = _hunks(f.hunks, DIFF_MAX_LINES)
    return {"path": f.path, "old": f.old_path, "status": f.status, "added": f.added,
            "removed": f.removed, "binary": f.binary, "generated": f.generated,
            "shown": shown, "why": why, "truncated": truncated or cut, "ref": ref or REF_ALL,
            "how": _how(m, ref, pf), "hunks": hunks}


# ---------------------------------------------------------------- the entry point the page calls

_cache = {}
_cache_lock = threading.Lock()
CLEAN_AFTER = 300.0


def _cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        for k in [k for k, v in _cache.items() if now - v[0] > CLEAN_AFTER]:
            _cache.pop(k, None)
    val = fn()
    if val.get("error"):
        return val            # never cache a failure: press refresh and it tries again
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


def summary(m, git, ref=REF_ALL):
    """What the Changes card shows for one view: the picker, the range, the file list."""
    jid = m.get("id")
    pf = _cached(("probe", jid), REFS_TTL, lambda: probe(m, git))
    if pf.get("error"):
        return {"ok": False, "error": pf["error"]}
    ref = ref or REF_ALL
    view = _cached(("list", jid, ref), SUMMARY_TTL, lambda: _list(m, git, ref, pf))
    if view.get("error"):
        return {"ok": False, "error": view["error"]}
    return {"ok": True, "ref": ref, "how": _how(m, ref, pf), "branch": pf["branch"],
            "refs": pf["refs"], "files": view["files"], "more": view["more"],
            "totals": view["totals"],
            "git_start": _base(m)[:8], "running": m.get("status") in ("queued", "running", "stopping")}


if __name__ == "__main__":   # quick manual look at one job folder: python3 diffview.py <folder> [ref]
    import sys

    class _G:
        repo = Path(sys.argv[1]).resolve()
        wrap = None
        timeout = 120

    print(json.dumps(summary({"id": "cli", "folder": "", "status": "done", "created": time.time()},
                             _G(), sys.argv[2] if len(sys.argv) > 2 else REF_ALL), indent=1)[:4000])
