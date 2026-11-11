#!/usr/bin/env python3
"""reviewbundle: pack a finished agent job into one compact Markdown review file.

One click on the job page copies this to Claude (a cloud AI) for code review.
Every line must help a reviewer: no logs, no noise.

    build_bundle(meta, events_path, git, jobs_lookup, max_chars=60000) -> str

  meta         the job's meta.json dict
  events_path  its events.jsonl
  git          callable git(script) -> str: runs a shell script inside the project
               folder and returns stdout. In jobboard.py this is
               `lambda s: in_sandbox(m["folder"], s, timeout=120)` - git only ever
               runs inside the sandbox, never on the host (the repo is untrusted).
               Each call starts a container, so build_bundle makes at most 6.
  jobs_lookup  id -> meta dict or None (walks the follow-up chain via meta["parent"])

Standard library only, no side effects at import, and no subprocess anywhere:
every read of the project folder goes through the git callable. Paths from the
repo are untrusted: they never enter a shell command string (the untracked
listing keeps them in quoted "$f" variables and hands them back base64'd).
"""
import base64
import fnmatch
import json
import re
import shlex
import time

import jobstatus

SHELL_TOOLS = {"Bash", "run_shell_command"}
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

LOCK_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
SKIP_DIRS = {"build", "dist", ".gradle", "node_modules", "__pycache__"}
REPORT_NAMES = {"AGENT_REPORT.md", "PLAN.md"}

CHAIN_MAX = 20
EARLIER_PROMPT_CHARS = 600
SUMMARY_CHARS = 4000
REPORT_CHARS = 6000
BT_GROUPS = 8
BT_FAIL_LINES = 25
BT_PASS_LINES = 3
BT_LINE_CHARS = 300      # one giant AssertionError repr must not eat the bundle
BT_CLIP_HEAD = 240
BT_CLIP_TAIL = 40
BIG_FILE_LINES = 400
UNTRACKED_MAX_LINES = 200
UNTRACKED_MAX_BYTES = 20000
UNTRACKED_SHOW = 20

# ---------------------------------------------------------------- text helpers

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(s):
    return _ANSI_RE.sub("", str(s or "")).replace("\r", "")


def _shorten(p, n):
    p = str(p or "")
    if len(p) <= n:
        return p
    return p[:n].rstrip() + f"\n... ({len(p) - n} more chars)"


def _dur(meta):
    st, fi = meta.get("started"), meta.get("finished")
    if not st:
        return ""
    secs = int((fi or time.time()) - st)
    return f"{secs // 60}m {secs % 60:02d}s"


def _chain(meta, jobs_lookup):
    """This job plus its ancestors, oldest first (cycle-guarded)."""
    chain, seen = [meta], {str(meta.get("id"))}
    cur = meta
    while cur and len(chain) < CHAIN_MAX:
        pid = cur.get("parent")
        if not pid or pid in seen:
            break
        seen.add(str(pid))
        try:
            cur = jobs_lookup(pid)
        except Exception:
            cur = None
        if cur:
            chain.append(cur)
    chain.reverse()
    return chain


# ---------------------------------------------------------------- events

def _read_events(events_path):
    try:
        f = open(str(events_path), encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                yield json.loads(raw)
            except ValueError:
                pass


def _result_event(events_path):
    out = None
    for e in _read_events(events_path):
        if e.get("type") == "result":
            out = {"text": str(e.get("result") or ""), "is_error": bool(e.get("is_error")),
                   "num_turns": e.get("num_turns"), "duration_ms": e.get("duration_ms") or 0}
    return out


_CD_PREFIX = re.compile(r"^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)+")
# jobstatus already says "Running tests"/"Building" for these; add the rest:
_EXTRA_BT = re.compile(r"\bpython3?(?:\.\d+)?\s+-m\s+py_compile\b|\bnode\s+--check\b|"
                       r"\bcargo\s+(?:build|test)\b|\bgo\s+build\b", re.I)


def _norm_cmd(cmd):
    return " ".join(_CD_PREFIX.sub("", str(cmd)).split())


# Grouping runs is by WHAT ran, not the exact text: `... -v 2>&1 | tail -4`
# and `... | tail -3 && git add -A ...` are the same suite run re-checked.
_PIPE_FILTERS = {"tail", "head", "grep", "sed", "awk", "tee", "cat", "wc",
                 "sort", "uniq", "cut"}
_NOT_BUILD_TEST = {"cd", "git", "echo", "ls", "export", "true", "mkdir",
                   "cp", "mv", "rm"}
_OUTPUT_FLAGS = {"-v", "-q", "--quiet", "--verbose", "--no-daemon", "--info",
                 "--stacktrace", "-x", "2>&1", ">/dev/null", "2>/dev/null"}
_OUTPUT_FLAG_RE = re.compile(r"^(?:--console=|--tb(?:=|$))")


def _split_ops(cmd):
    """Split a command line on && || ; | outside quotes. Approximation is
    fine: the goal is naming what ran, not executing anything."""
    parts, tok, quote, i = [], [], "", 0
    s = str(cmd)
    while i < len(s):
        ch = s[i]
        if quote:
            tok.append(ch)
            if ch == quote:
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            tok.append(ch)
            i += 1
            continue
        pair = s[i:i + 2]
        if pair in ("&&", "||") or ch in ";|":
            parts.append("".join(tok))
            tok = []
            i += 2 if pair in ("&&", "||") else 1
            continue
        tok.append(ch)
        i += 1
    parts.append("".join(tok))
    return [p for p in parts if p.strip()]


def _bt_key(cmd):
    """The group key: strip pipe-filter stages and non-build/test parts,
    strip output-only flags, and use the LAST remaining build/test part -
    a run like `py_compile ... && unittest ...` ends with the unittest
    output, so it belongs to the unittest group."""
    last = None
    for part in _split_ops(_norm_cmd(cmd)):
        try:
            toks = shlex.split(part)
        except ValueError:
            toks = part.split()
        if not toks:
            continue
        if toks[0].split("/")[-1] in _PIPE_FILTERS or \
           toks[0].split("/")[-1] in _NOT_BUILD_TEST:
            continue
        kept = [t for t in toks
                if t not in _OUTPUT_FLAGS and not _OUTPUT_FLAG_RE.match(t)]
        if kept:
            last = " ".join(kept)
    return last


def _looks_build_test(name, cmd):
    """Reuse jobstatus's classifier, plus py_compile / node --check / cargo / go build.
    Heredoc commands (python3 - <<'EOF' writing a file that happens to mention
    ./gradlew) are the agent writing files, not a build: skip them."""
    if name not in SHELL_TOOLS or not cmd.strip():
        return False
    if "<<" in cmd:
        return False
    if jobstatus.current_action(("tool", name, {"command": cmd})) in ("Running tests", "Building"):
        return True
    return bool(_EXTRA_BT.search(cmd))


def _result_text(c):
    body = c.get("content")
    if isinstance(body, list):
        body = "\n".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in body)
    return strip_ansi(str(body or ""))


def _collect_runs(events_path):
    """Pair tool_use -> tool_result by tool_use_id; keep build/test commands.
    Group by WHAT ran (_bt_key), not the exact text; only the LAST run of each
    group is shown, with a note when earlier runs of the group failed; max
    BT_GROUPS, in order of last run."""
    calls, runs = {}, []
    for e in _read_events(events_path):
        t = e.get("type")
        if t == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in SHELL_TOOLS:
                    inp = c.get("input") if isinstance(c.get("input"), dict) else {}
                    cmd = str(inp.get("command") or "")
                    if cmd.strip():
                        calls[c.get("id")] = (c.get("name"), cmd)
        elif t == "user":
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    key = c.get("tool_use_id")
                    if key in calls:
                        name, cmd = calls.pop(key)
                        if _looks_build_test(name, cmd):
                            runs.append((cmd, _result_text(c), bool(c.get("is_error"))))
    groups, order = {}, []
    for cmd, text, err in runs:
        k = _bt_key(cmd)
        if not k:
            continue
        if k in groups:
            order.remove(k)
        else:
            groups[k] = {"raw": "", "runs": []}
        order.append(k)
        groups[k]["raw"] = _norm_cmd(cmd)
        groups[k]["runs"].append((text, err))
    out = []
    for k in order[-BT_GROUPS:]:
        g = groups[k]
        text, err = g["runs"][-1]
        out.append({"cmd": k, "raw": g["raw"], "text": text, "err": err,
                    "n": len(g["runs"]),
                    "fail": _failed(text, err),
                    "fails": sum(1 for t, e in g["runs"][:-1] if _failed(t, e))})
    return out


# ------------------------------------------------------------ pass / fail calls
# Explicit summary lines decide first (test output often prints "Traceback" or
# "error:" on purpose when it checks error handling). The word scan is only the
# fallback when no clear summary exists. is_error from the tool is always FAIL.

_FAIL_RES = [re.compile(r"\bFAILED\b"), re.compile(r"\bTraceback\b"),
             re.compile(r"\berror:"), re.compile(r"BUILD FAILED")]
_EXIT_RES = re.compile(r"\bexit(?:\s*code)?[^0-9\n]{0,3}[1-9]\d*\b", re.I)

_EXIT_SUM = re.compile(r"^(?:EXIT=|exit (?:code|status) )(\d+)$", re.I)
_SUM_RES = [
    re.compile(r"^Ran \d+ tests?(?: in .*)?$"),
    re.compile(r"^OK(?: \(.*\))?$"),
    re.compile(r"^FAILED \(.*\)$"),
    re.compile(r"^BUILD (?:SUCCESSFUL|FAILED)"),
    re.compile(r"^FAILURE:"),
    re.compile(r"^\d+ (?:passed|failed|errors|expected failures)\b.*"),
    re.compile(r"^test result: \w+\..*"),
    _EXIT_SUM,
]


def _sum_is_fail(s):
    if re.match(r"^(FAILED|BUILD FAILED|FAILURE)", s):
        return True
    if re.match(r"^\d+ (?:failed|errors)\b", s):
        return True
    m = _EXIT_SUM.match(s)
    return bool(m) and m.group(1) != "0"


def _sum_is_pass(s):
    if re.match(r"^OK(?: \(.*\))?$", s) or re.match(r"^Ran \d+ tests?(?: in .*)?$", s):
        return True
    if s.startswith("BUILD SUCCESSFUL") or s.startswith("test result: ok"):
        return True
    if re.match(r"^\d+ passed\b", s) and "fail" not in s.lower() and "error" not in s.lower():
        return True
    m = _EXIT_SUM.match(s)
    return bool(m) and m.group(1) == "0"


def _failed(text, is_error):
    """FAIL? is_error always; the LAST explicit summary line decides; only
    without any summary line fall back to the word scan."""
    if is_error:
        return True
    last = None
    for ln in text.splitlines():
        s = ln.strip().strip("=").strip()
        if any(r.match(s) for r in _SUM_RES):
            last = s
    if last is not None:
        if _sum_is_fail(last):
            return True
        if _sum_is_pass(last):
            return False
    if _EXIT_RES.search(text):
        return True
    return any(r.search(text) for r in _FAIL_RES)


_ERR_RES = re.compile(r"FAILED|FAILURE|[Ee]rror:|\bERROR\b|^e: |\bTraceback\b|\bException\b")


def _summary_lines(text, cap):
    out = []
    for ln in text.splitlines():
        s = ln.strip().strip("=").strip()
        if any(r.match(s) for r in _SUM_RES):
            out.append(ln.rstrip())
    return out[:cap]


def _fail_block(text):
    """Error lines plus 2 lines of context, max BT_FAIL_LINES lines."""
    lines = text.splitlines()
    keep = set()
    for i, ln in enumerate(lines):
        s = ln.strip()
        if _ERR_RES.search(s) or any(r.match(s.strip("=").strip()) for r in _SUM_RES):
            for j in range(max(0, i - 2), min(len(lines), i + 3)):
                keep.add(j)
    idx = sorted(keep)
    cut = len(idx) > BT_FAIL_LINES
    idx = idx[:BT_FAIL_LINES]
    block = [lines[i].rstrip() for i in idx]
    if cut:
        block.append(f"(... error lines continue, {len(keep) - BT_FAIL_LINES} more not shown)")
    if not block:
        block = ["(failure detected but no error line matched)"]
    return block


def _clip_line(ln):
    """Cap one output line: keep the start and the end, mark the middle."""
    if len(ln) <= BT_LINE_CHARS:
        return ln
    return (ln[:BT_CLIP_HEAD]
            + f" ... ({len(ln) - BT_CLIP_HEAD - BT_CLIP_TAIL} chars cut) ... "
            + ln[-BT_CLIP_TAIL:])


def _fence(block):
    # keep the fence safe: a stray ``` inside would break the Markdown;
    # and cap every line, so one multi-KB repr cannot dominate the bundle
    block = [_clip_line(ln.replace("```", "'''")) for ln in block]
    return "```text\n" + "\n".join(block) + "\n```"


def _bt_section(runs):
    if not runs:
        return None
    out = []
    for r in runs:
        fail = r["fail"]
        cmd = r["cmd"].replace("`", " ").replace("\n", " ")
        if len(cmd) > 120:  # keep the line scannable; the result block tells the story
            cmd = cmd[:117].rstrip() + "..."
        if r["n"] > 1:
            times = f"ran {r['n']} times"
            if r["fails"] and not fail:
                times += f", failed {r['fails']} {'time' if r['fails'] == 1 else 'times'} before passing"
            note = f" ({times}, last shown)"
        else:
            note = ""
        last = ""
        if r["raw"] and r["raw"] != r["cmd"]:  # group key hides re-check flags/pipes
            raw = r["raw"].replace("`", " ").replace("\n", " ")
            if len(raw) > 120:
                raw = raw[:117].rstrip() + "..."
            last = f" (last: {raw})"
        out.append(f"- {'FAIL' if fail else 'PASS'} `{cmd}`{note}{last}")
        if fail:
            out.append(_fence(_fail_block(r["text"])))
        else:
            block = _summary_lines(r["text"], BT_PASS_LINES)
            if block:  # a PASS with nothing to say gets no empty block at all
                out.append(_fence(block))
    return "## Build and test results\n\n" + "\n".join(out)


# ---------------------------------------------------------------- diff

def _skip(path):
    parts = path.split("/")
    base = parts[-1]
    if base in LOCK_NAMES or base.endswith(".lock") or base.endswith("-wrapper.jar"):
        return True
    if base in REPORT_NAMES:
        return True
    return any(p in SKIP_DIRS for p in parts[:-1])


_TEST_DIR_RE = re.compile(r"(^|/)(?:tests?|__tests__|androidTest)/")
_TEST_NAME_RE = re.compile(r"(?:^test_.*\.py$|_test\.py$|Tests?\.kt$|\.(?:test|spec)\.(?:js|ts)$)", re.I)


def _is_test(path):
    """A test file: the size cap cuts these before the real source diffs."""
    return bool(_TEST_DIR_RE.search(path) or _TEST_NAME_RE.search(path.split("/")[-1]))


# ------------------------------------------------------- .reviewignore + value tiers

def _parse_ignore(text):
    """.reviewignore: one pattern per line, # comments, trailing '/' = directory."""
    out = []
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def _ignore_match(path, pats):
    """Does a repo path match one of the .reviewignore patterns? The pattern is
    fnmatch'd against the whole path AND every path prefix, so 'ref-copy/' also
    covers 'ref-copy/sub/x.py'. Untrusted text only ever reaches fnmatch."""
    parts = str(path).split("/")
    for p in pats:
        q = p.rstrip("/")
        if not q:
            continue
        for i in range(1, len(parts) + 1):
            if fnmatch.fnmatchcase("/".join(parts[:i]), q):
                return True
    return False


def _ignore_stats(pats, secs):
    """Per-pattern totals for the collapsed stat lines: files, +added, -removed.
    Each section counts under the FIRST pattern that matches it."""
    agg = {}
    for s in secs:
        pat = next(p for p in pats if _ignore_match(s["path"], [p]))
        a = agg.setdefault(pat, [0, 0, 0])
        a[0] += 1
        for ln in s["lines"]:
            if ln.startswith("+") and not ln.startswith("+++"):
                a[1] += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                a[2] += 1
    return agg


_LOW_DIRS = {"vendor", "third_party", "node_modules"}
_DATA_EXT = (".md", ".txt", ".json", ".csv", ".xml", ".svg")


def _low_value(path):
    """Review value last: vendored code, reference copies (vendor/, third_party/,
    node_modules/, any directory named reference or ending -ref, like bench-ref/),
    minified/mapped JS, and data/doc files (.md .txt .json .csv .xml .svg) that
    are NOT the project's own top-level files."""
    parts = str(path).split("/")
    dirs, base = parts[:-1], parts[-1]
    if any(d in _LOW_DIRS or d == "reference" or d.endswith("-ref") for d in dirs):
        return True
    if base.endswith(".min.js") or base.endswith(".map"):
        return True
    return len(parts) > 1 and base.endswith(_DATA_EXT)


def _parse_numstat(txt):
    """git --numstat lines -> {path: (added, removed)}; '-' (binary) counts 0.
    Rename paths ('a/{x => y}.py', 'old => new') keep the new name."""
    out = {}
    for ln in txt.splitlines():
        p = ln.split("\t")
        if len(p) < 3:
            continue
        try:
            a = int(p[0]) if p[0] != "-" else 0
            d = int(p[1]) if p[1] != "-" else 0
        except ValueError:
            continue
        path = "\t".join(p[2:])
        if " => " in path:
            path = re.sub(r"\{[^{}]*=>[^{}]*\}",
                          lambda m: m.group(0)[1:-1].split("=>")[1].strip(), path)
            if " => " in path:
                path = path.split(" => ")[-1]
            path = path.replace("//", "/")
        out[path] = (a, d)
    return out


_H_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)")


def _finish(sec):
    lines = sec["lines"]
    n = sum(1 for ln in lines
            if (ln.startswith("+") and not ln.startswith("+++"))
            or (ln.startswith("-") and not ln.startswith("---")))
    sec["n_changed"] = n
    sec["has_hunk"] = any(ln.startswith("@@") for ln in lines)
    ren = [ln for ln in lines if ln.startswith("rename from ") or ln.startswith("rename to ")]
    sec["pure_rename"] = bool(ren) and not sec["has_hunk"] and not n
    sec["is_new"] = any(ln.startswith("new file mode") for ln in lines)
    return sec


def _split_sections(body):
    secs, cur = [], None
    for ln in body.splitlines():
        m = _H_RE.match(ln)
        if m:
            if cur:
                secs.append(_finish(cur))
            cur = {"old": m.group(1), "path": m.group(2), "lines": [ln]}
        elif cur is not None:
            cur["lines"].append(ln)
    if cur:
        secs.append(_finish(cur))
    return secs


# One call lists every untracked file: names never enter the command string
# (they stay in the quoted "$f" loop variable) and come back base64'd so no
# content can forge a delimiter line. '@@U <b64 path>' / '@@L <lines> <bytes>'
# / optional base64 content, then '@@T <total>'.
UNTRACKED_SCRIPT = """
git ls-files --others --exclude-standard -z 2>/dev/null | {
  i=0
  while IFS= read -r -d '' f; do
    case "$f" in
      build/*|*/build/*|dist/*|*/dist/*|.gradle/*|*/.gradle/*|node_modules/*|*/node_modules/*|__pycache__/*|*/__pycache__/*) continue ;;
    esac
    b=${f##*/}
    case "$b" in
      package-lock.json|yarn.lock|pnpm-lock.yaml|*.lock|*-wrapper.jar|AGENT_REPORT.md|PLAN.md) continue ;;
    esac
    i=$((i+1))
    if [ "$i" -le %d ]; then
      printf '@@U %%s\\n' "$(printf '%%s' "$f" | base64 | tr -d '\\n')"
      lc=$(wc -l < "$f" 2>/dev/null | tr -d ' ')
      sz=$(wc -c < "$f" 2>/dev/null | tr -d ' ')
      printf '@@L %%s %%s\\n' "${lc:--1}" "${sz:--1}"
      if [ "${lc:--1}" -ge 0 ] 2>/dev/null && [ "${lc:--1}" -lt %d ] \\
         && [ "${sz:--1}" -ge 0 ] && [ "${sz:--1}" -lt %d ]; then
        base64 < "$f" | tr -d '\\n'; printf '\\n'
      fi
    fi
  done
  printf '@@T %%s\\n' "$i"
}
""" % (UNTRACKED_SHOW, UNTRACKED_MAX_LINES, UNTRACKED_MAX_BYTES)


def _b64d(s):
    try:
        return base64.b64decode(s).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return None


def _untracked_listing(out):
    """Parse the one-call listing into sections plus the untracked total."""
    secs, lines = [], out.splitlines()
    total = 0
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("@@U "):
            path = _b64d(ln[4:].strip())
            lc = sz = -1
            if i + 1 < len(lines) and lines[i + 1].startswith("@@L "):
                p = lines[i + 1][4:].split()
                try:
                    lc, sz = int(p[0]), int(p[1])
                except (ValueError, IndexError):
                    lc = sz = -1
            content = None
            step = 2
            if i + 2 < len(lines) and not lines[i + 2].startswith("@@"):
                content = _b64d(lines[i + 2])
                step = 3
            i += step
            if path is None or _skip(path):
                continue
            if lc < 0:
                secs.append({"path": path, "lines": [f"(new untracked file: {path}: unreadable, not shown)"],
                             "n_changed": 0, "has_hunk": False, "pure_rename": False, "is_new": True})
                continue
            if lc >= UNTRACKED_MAX_LINES or (sz >= UNTRACKED_MAX_BYTES and content is None):
                secs.append({"path": path, "lines": [f"(new untracked file: {path}: {lc} lines, too big to include)"],
                             "n_changed": lc, "has_hunk": False, "pure_rename": False, "is_new": True})
                continue
            if content is None or "\x00" in content:
                secs.append({"path": path, "lines": [f"(new untracked file: {path}: binary, not shown)"],
                             "n_changed": lc, "has_hunk": False, "pure_rename": False, "is_new": True})
                continue
            body = [f"diff --git a/{path} b/{path}", f"(new untracked file: +{lc} lines)"]
            body += ["+" + cl for cl in content.splitlines()]
            secs.append({"path": path, "lines": body, "n_changed": lc,
                         "has_hunk": True, "pure_rename": False, "is_new": True})
        elif ln.startswith("@@T "):
            try:
                total = int(ln[4:].strip())
            except ValueError:
                pass
            i += 1
        else:
            i += 1
    return secs, total


def _collect_diff(meta, git):
    """Find the job's changes. ONE probe call gets: is-it-a-git-repo, the current
    branch, the window commits, the diff base and .reviewignore (committed +
    working copy). Then one call for stat AND numstat, one for the body and
    (git_start path) the untracked listing: 6 git calls maximum in total."""
    res = {"how": "", "stat": "", "files": [], "git": False, "none": True, "branch_now": "",
           "ignore": [], "ignored": {}, "numstat": {}}
    started = meta.get("started") or meta.get("created")
    lo = int(started - 5) if started else 0
    hi = int((meta.get("finished") or time.time()) + 120)
    # the same call also fetches .reviewignore: committed version, and the
    # working copy (a job may have written it itself without committing)
    probe = git("git rev-parse --is-inside-work-tree 2>/dev/null; echo '@@BR'; "
                "git rev-parse --abbrev-ref HEAD 2>/dev/null; echo '@@LOG'; "
                f"shas=$(git log --all --format=\"%H %ct\" 2>/dev/null "
                f"| awk -v lo={lo} -v hi={hi} '$2>=lo && $2<=hi{{print $1}}'); "
                "echo \"$shas\"; echo '@@BASE'; "
                "if [ -n \"$shas\" ]; then git rev-parse --verify \"$(echo \"$shas\" | tail -n 1)^\" 2>/dev/null; fi; "
                "echo '@@IGN'; git show HEAD:.reviewignore 2>/dev/null; "
                "echo '@@IGNWC'; cat .reviewignore 2>/dev/null")
    if probe.startswith("(could not read"):
        res["how"] = probe
        return res
    seg, key = ({"CHK": "", "BR": "", "LOG": "", "BASE": "", "IGN": "", "IGNWC": ""}), "CHK"
    for ln in probe.splitlines():
        if ln.strip() in ("@@BR", "@@LOG", "@@BASE", "@@IGN", "@@IGNWC"):
            key = ln.strip()[2:]
            continue
        seg[key] += ln + "\n"
    if "true" not in seg["CHK"]:
        res["how"] = "not a git repository"
        return res
    res["git"] = True
    bn = seg["BR"].strip().splitlines()[0].strip() if seg["BR"].strip() else ""
    if bn and len(bn) < 80 and " " not in bn:
        res["branch_now"] = bn
    ign = seg["IGNWC"].strip() or seg["IGN"].strip()   # uncommitted copy wins
    if ign:
        res["ignore"] = _parse_ignore(ign)
    gs = str(meta.get("git_start") or "")
    if SHA_RE.fullmatch(gs):
        res["how"] = f"start commit {gs[:9]} recorded when the job began, plus uncommitted changes"
        rng, untracked = gs, git(UNTRACKED_SCRIPT)
        body_cmd = f"git diff -U0 --no-color --no-ext-diff --ignore-cr-at-eol {gs} 2>/dev/null"
    else:
        shas = [s for s in seg["LOG"].split() if SHA_RE.fullmatch(s)]
        if shas:
            newest, oldest = shas[0], shas[-1]
            base = seg["BASE"].strip().splitlines()[0].strip() if seg["BASE"].strip() else ""
            if not SHA_RE.fullmatch(base):
                base = EMPTY_TREE
            word = "commit" if len(shas) == 1 else "commits"
            res["how"] = (f"{len(shas)} {word} dated inside the job window, "
                          f"diff {base[:9]}..{newest[:9]}")
            rng, untracked = f"{base} {newest}", ""
            body_cmd = f"git diff -U0 --no-color --no-ext-diff --ignore-cr-at-eol {base} {newest} 2>/dev/null"
        else:
            res["how"] = "uncommitted changes only (no commits dated inside the job window)"
            rng, untracked = "HEAD", ""
            body_cmd = f"git diff -U0 --no-color --no-ext-diff --ignore-cr-at-eol HEAD 2>/dev/null"
    # one call carries --stat AND --numstat (the collapsed group lines need the
    # added/removed counts); the marker rides on its own line
    raw = git(f"git diff --stat --stat-width=100 {rng} 2>/dev/null; echo '@@NS'; "
              f"git diff --numstat {rng} 2>/dev/null")
    ls = raw.splitlines()
    i = ls.index("@@NS") if "@@NS" in ls else len(ls)
    res["stat"] = "\n".join(ls[:i])
    res["numstat"] = _parse_numstat("\n".join(ls[i + 1:]))
    files = _split_sections(git(body_cmd))
    if untracked and not untracked.startswith("(could not read"):
        u_secs, u_total = _untracked_listing(untracked)
        files += u_secs
        if u_total > UNTRACKED_SHOW:
            files.append({"path": "@untracked-more",
                          "lines": [f"(and {u_total - UNTRACKED_SHOW} more untracked files not shown)"],
                          "n_changed": 0, "has_hunk": False, "pure_rename": False, "is_new": False})
    if res["ignore"]:
        ig = [s for s in files if s["path"] != "@untracked-more"
              and _ignore_match(s["path"], res["ignore"])]
        if ig:
            files = [s for s in files if s not in ig]
            res["ignored"] = _ignore_stats(res["ignore"], ig)
    res["files"] = files
    res["none"] = not res["stat"].strip() and not files
    return res


def _stat_display(diff):
    """The stat block as shown: git's own lines, with .reviewignore files
    replaced by one collapsed line per pattern, and - only when the file list
    runs past 40 lines - whole directories that changed a lot replaced by one
    line each. git's trailing "N files changed, ..." summary stays at the end."""
    lines = diff["stat"].strip("\n").splitlines()
    pats, ns = diff.get("ignore") or [], diff.get("numstat") or {}

    def path_of(ln):
        return ln.split("|", 1)[0].strip().strip('"')

    def real_of(ln):
        """git --stat abbreviates long names ('...st/tests/test_core.py'); the
        numstat paths (never abbreviated) give back the real one."""
        p = path_of(ln)
        if not p.startswith("..."):
            return p
        return next((q for q in ns if q.endswith(p[3:])), p)

    files, tail = [], []
    for ln in lines:
        (files if "|" in ln else tail).append(ln)
    if pats:
        files = [ln for ln in files if not _ignore_match(real_of(ln), pats)]
    out = list(files)
    if len(files) > 40:                        # big folder -> one line per folder
        by_dir = {}
        for ln in files:
            p = real_of(ln)
            if "/" in p:
                by_dir.setdefault(p.split("/")[0], []).append(ln)
        for top in sorted(by_dir):
            group = by_dir[top]
            if len(group) <= 8:
                continue
            out = [ln for ln in out if ln not in group]
            a = sum(ns.get(real_of(ln), (0, 0))[0] for ln in group)
            r = sum(ns.get(real_of(ln), (0, 0))[1] for ln in group)
            out.append(f"{top}/ ({len(group)} files, +{a:,} -{r:,})")
    for pat, (n, a, d) in (diff.get("ignored") or {}).items():
        ad = f"+{a:,} -{d:,}" if d else f"+{a:,}"
        out.append(f"{pat} ({n} files, {ad} lines - in .reviewignore, not shown)")
    return "\n".join(out + tail)


def _diff_md(diff, omit):
    out = ["## Diff", ""]
    if diff["none"]:
        out += [f"Range: {diff['how']}.", "", "no changes found"]
        return "\n".join(out)
    out += [f"Range: {diff['how']}.", "", "```", _stat_display(diff), "```", ""]
    body = []
    for sec in diff["files"]:
        if _skip(sec["path"]):
            continue
        if sec["pure_rename"]:
            body.append(f"(renamed: {sec['old']} -> {sec['path']}, no content changes)")
            continue
        if sec["path"] in omit:
            body.append(f"({sec['path']}: {sec['n_changed']} lines changed, "
                        "omitted to fit the size limit - ask for it)")
            continue
        if len(sec["lines"]) > BIG_FILE_LINES:
            body.append(f"({sec['path']}: {sec['n_changed']} lines changed, "
                        "over 400 lines, omitted - ask for it)")
            continue
        body.extend(sec["lines"])
    out += ["```", "\n".join(body).strip("\n"), "```"] if body else ["(only filtered files changed: lockfiles, build output or reports)"]
    return "\n".join(out)


# ---------------------------------------------------------------- main

def build_bundle(meta, events_path, git, jobs_lookup, max_chars=60000):
    meta = meta or {}
    mid = str(meta.get("id") or "job")
    chain = _chain(meta, jobs_lookup)
    result = _result_event(events_path)

    # ---- diff first: the header reports how the range was found
    diff = _collect_diff(meta, git)

    branch = str(meta.get("git_branch_start") or "")
    branch_now = not branch and diff["branch_now"]
    if branch_now:
        branch = diff["branch_now"]  # older jobs: only the current branch is knowable

    # ---- header
    bullets = [f"- mode: {meta.get('mode', '?')}",
               f"- folder: {meta.get('folder', '?')}",
               f"- status: {meta.get('status', '?')}" +
               (f" (exit {meta['exit']})" if meta.get("exit") is not None else "")]
    if branch:
        bullets.insert(2, f"- branch{' at start' if not branch_now else ' (now)'}: {branch}")
    d = _dur(meta)
    if d:
        bullets.append(f"- duration: {d}")
    if result and result.get("num_turns") is not None:
        bullets.append(f"- turns: {result['num_turns']}")
    bullets.append(f"- diff range: {diff['how']}")
    header = f"# Review bundle: {mid}\n\n" + "\n".join(bullets)

    # ---- job prompt (with the earlier prompts of a follow-up chain)
    prompts = {}  # id -> text (mutable for shrinking)
    for m in chain:
        jid = str(m.get("id"))
        text = str(m.get("job") or "")
        if jid != mid:  # earlier prompts start out trimmed, see the shrink loop below
            text = _shorten(text, EARLIER_PROMPT_CHARS)
        prompts[jid] = text

    def prompts_md():
        if len(chain) == 1:
            return "## Job prompt\n\n" + prompts[mid]
        parts = ["## Job prompt"]
        for m in chain:
            jid = str(m.get("id"))
            tag = "this job" if jid == mid else "earlier job"
            parts.append(f"\n### {tag} {jid}\n\n{prompts[jid]}")
        return "\n".join(parts)

    # ---- agent's summary + AGENT_REPORT.md. The report is cumulative across
    # follow-ups, so when it shows up in this job's diff as a MODIFIED file we
    # show only the lines this job added; new file / no git -> the whole file
    # when its mtime is inside the job window.
    summary_md = None
    if result and result["text"].strip():
        text = strip_ansi(result["text"])
        if len(text) > SUMMARY_CHARS:
            text = text[:SUMMARY_CHARS].rstrip() + f"\n... (summary cut at {SUMMARY_CHARS} chars)"
        summary_md = "## Agent's summary\n\n" + text

    report, report_title = "", "### AGENT_REPORT.md"
    rep_sec = next((s for s in diff["files"]
                    if s["path"] == "AGENT_REPORT.md" and s["has_hunk"] and not s["is_new"]), None)
    if rep_sec is not None:
        added = [ln[1:] for ln in rep_sec["lines"] if ln.startswith("+") and not ln.startswith("+++")]
        report = strip_ansi("\n".join(added)).strip()
        report_title = "### AGENT_REPORT.md (this job's additions)"
    else:
        report_mt = None
        mt_out = git("stat -c %Y AGENT_REPORT.md 2>/dev/null || stat -f %m AGENT_REPORT.md 2>/dev/null").strip()
        if mt_out.isdigit():
            report_mt = int(mt_out)
        if report_mt is not None and jobstatus.mtime_in_window(meta, report_mt):
            report = strip_ansi(git("cat AGENT_REPORT.md 2>/dev/null")).strip()
    if len(report) > REPORT_CHARS:
        report = report[:REPORT_CHARS].rstrip() + f"\n... (report cut at {REPORT_CHARS} chars)"
    report_cut = None

    def report_md():
        if report_cut == "drop":
            return report_title + "\n\n(AGENT_REPORT.md omitted to fit)"
        txt = report if report_cut != "short" else _shorten(report, 1500) + "\n(AGENT_REPORT.md cut to fit)"
        return report_title + "\n\n" + txt

    # ---- build and test results
    bt_md = _bt_section(_collect_runs(events_path))

    # ---- shrink order: earlier chain prompts, AGENT_REPORT, then diffs:
    # low-review-value files (vendored/reference/minified/data) biggest first,
    # then test files biggest first, then real code files SMALLEST first - big
    # core files survive longest. The job prompt, the summary and the build/test
    # results are never cut.
    actions = []
    for m in chain:
        jid = str(m.get("id"))
        if jid != mid and len(prompts[jid]) > 200:
            actions.append(("shorten", jid))
    for m in chain:
        jid = str(m.get("id"))
        if jid != mid and len(prompts[jid]) > 200:
            actions.append(("drop", jid))
    if report:
        actions.append(("report_short", None))
        actions.append(("report_drop", None))
    shown = [s for s in diff["files"]
             if not _skip(s["path"]) and s["has_hunk"] and not s["pure_rename"]
             and len(s["lines"]) <= BIG_FILE_LINES]
    size = lambda s: len("\n".join(s["lines"]))

    def _rank(pred, biggest_first):
        return sorted((s for s in shown if pred(s["path"])), key=size, reverse=biggest_first)

    for sec in _rank(_low_value, True):
        actions.append(("omit", sec["path"]))
    for sec in _rank(lambda p: not _low_value(p) and _is_test(p), True):
        actions.append(("omit", sec["path"]))
    for sec in _rank(lambda p: not _low_value(p) and not _is_test(p), False):
        actions.append(("omit", sec["path"]))
    omit = set()

    def render():
        secs = [header, prompts_md()]
        s = summary_md
        if s:
            secs.append(s + ("\n\n" + report_md() if report or report_cut else ""))
        if bt_md:
            secs.append(bt_md)
        secs.append(_diff_md(diff, omit))
        return "\n\n".join(secs)

    def total():
        t = render()
        return len(t) + len(f"\n(bundle: {len(t)} chars, about {round(len(t) / 4)} tokens)")

    while total() > max_chars and actions:
        kind, arg = actions.pop(0)
        if kind == "shorten":
            prompts[arg] = _shorten(prompts[arg], 150) + "\n(earlier prompt shortened to fit)"
        elif kind == "drop":
            prompts[arg] = f"(earlier prompt for {arg} omitted to fit)"
        elif kind == "report_short":
            report_cut = "short"
        elif kind == "report_drop":
            report_cut = "drop"
        elif kind == "omit":
            omit.add(arg)

    body = render()
    # the footer counts its own bytes: iterate until the number is stable
    n = len(body)
    while True:
        foot = f"\n(bundle: {n} chars, about {round(n / 4)} tokens)"
        if len(body) + len(foot) == n:
            break
        n = len(body) + len(foot)
    return body + foot
