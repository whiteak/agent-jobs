"""Upgrade 1a: find, run and parse a project's build/test checks.

Standard library only. The actual command execution goes through a
`wrap` callable so the runner can put every check inside the same jail the
agent uses (agent-jail.sh --project <dir> -- <cmd>). Checks never get more
access than the agent.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

CHECKS_FILE = "AGENT_CHECKS.json"
DEFAULT_TIMEOUT = 1200


@dataclass
class Check:
    name: str
    cmd: str
    timeout_s: int = DEFAULT_TIMEOUT
    type: str = "command"          # "command" | "vision" | "android-smoke"
    extra: dict = field(default_factory=dict)


@dataclass
class CheckResult:
    name: str
    cmd: str
    exit_code: Optional[int]
    passed: Optional[int] = None
    failed: Optional[int] = None
    skipped: Optional[int] = None
    seconds: float = 0.0
    output_file: str = ""
    timed_out: bool = False
    first_error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not (self.failed or 0)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


# ---------------------------------------------------------------- loading

def load_checks(project: Path) -> tuple[list[Check], str]:
    """Return (checks, source). source is 'file', 'detected' or 'none'."""
    project = Path(project)
    f = project / CHECKS_FILE
    if f.is_file():
        data = json.loads(f.read_text(encoding="utf-8"))
        items = data.get("checks", data if isinstance(data, list) else [])
        checks = []
        for i, c in enumerate(items):
            known = {"name", "cmd", "timeout_s", "type"}
            checks.append(Check(
                name=str(c.get("name") or f"check{i + 1}"),
                cmd=str(c.get("cmd", "")),
                timeout_s=int(c.get("timeout_s", DEFAULT_TIMEOUT)),
                type=str(c.get("type", "command")),
                extra={k: v for k, v in c.items() if k not in known},
            ))
        return checks, "file"
    checks = detect_checks(project)
    return checks, ("detected" if checks else "none")


def write_checks(project: Path, checks: list[Check]) -> Path:
    out = {"checks": []}
    for c in checks:
        d = {"name": c.name, "cmd": c.cmd, "timeout_s": c.timeout_s}
        if c.type != "command":
            d["type"] = c.type
        d.update(c.extra)
        out["checks"].append(d)
    p = Path(project) / CHECKS_FILE
    p.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return p


def _has_pytest() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("pytest") is not None
    except Exception:
        return False


def _is_android_app(p: Path) -> bool:
    """A real Android app, not just any Gradle project (a launcher activity to start)."""
    for pattern in ("app/src/main/AndroidManifest.xml", "*/src/main/AndroidManifest.xml",
                    "AndroidManifest.xml"):
        if list(p.glob(pattern)):
            return True
    return False


def detect_checks(project: Path) -> list[Check]:
    p = Path(project)
    checks: list[Check] = []
    if (p / "gradlew").is_file():
        checks.append(Check("build", "./gradlew --offline assembleDebug"))
        checks.append(Check("tests", "./gradlew --offline testDebugUnitTest"))
        if _is_android_app(p):            # U06b: an app has to actually start on a device
            checks.append(Check("app runs", "", type="android-smoke", extra={"mode": "required"}))
        return checks
    pkg = p / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {}) or {}
        except (ValueError, OSError):
            scripts = {}
        if "build" in scripts:
            checks.append(Check("build", "npm run build"))
        test = scripts.get("test", "")
        if test and "no test specified" not in test:
            checks.append(Check("tests", "npm test"))
        if checks:
            return checks
    if (p / "Cargo.toml").is_file():
        return [Check("build", "cargo build --offline"), Check("tests", "cargo test --offline")]
    if list(p.glob("*.sln")) or list(p.glob("*.csproj")):
        return [Check("build", "dotnet build"), Check("tests", "dotnet test")]
    mk = p / "Makefile"
    if mk.is_file() and re.search(r"^test\s*:", mk.read_text(errors="replace"), re.M):
        return [Check("tests", "make test")]
    has_py_tests = bool(list(p.glob("test_*.py")) or list(p.glob("tests/test_*.py"))
                        or list(p.glob("tests/**/test_*.py")))
    if has_py_tests:
        if _has_pytest():
            return [Check("tests", "python3 -m pytest -q")]
        start = "tests" if (p / "tests").is_dir() else "."
        return [Check("tests", f"python3 -m unittest discover -s {start} -p 'test_*.py'")]
    return []


# ---------------------------------------------------------------- parsing

_PYTEST_RE = re.compile(r"=+ (.*?) in [\d.]+s", re.M)
_COUNT_RE = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")


def parse_counts(output: str, project: Optional[Path] = None,
                 since: Optional[float] = None) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Best effort (passed, failed, skipped) from tool output or JUnit XML."""
    # pytest final summary line
    m = None
    for m in _PYTEST_RE.finditer(output):
        pass
    if m and _COUNT_RE.search(m.group(1)):
        c = {"passed": 0, "failed": 0, "skipped": 0}
        for n, kind in _COUNT_RE.findall(m.group(1)):
            n = int(n)
            if kind in ("passed", "xpassed"):
                c["passed"] += n
            elif kind in ("failed", "error", "errors"):
                c["failed"] += n
            else:
                c["skipped"] += n
        return c["passed"], c["failed"], c["skipped"]
    # unittest
    m = re.search(r"^Ran (\d+) tests? in", output, re.M)
    if m:
        total = int(m.group(1))
        failed = skipped = 0
        fm = re.search(r"^FAILED \((.*?)\)", output, re.M)
        if fm:
            for k, v in re.findall(r"(failures|errors|skipped)=(\d+)", fm.group(1)):
                if k == "skipped":
                    skipped += int(v)
                else:
                    failed += int(v)
        om = re.search(r"^OK \((.*?)\)", output, re.M)
        if om:
            sm = re.search(r"skipped=(\d+)", om.group(1))
            if sm:
                skipped = int(sm.group(1))
        return total - failed - skipped, failed, skipped
    # jest
    m = re.search(r"^Tests:\s+(.*?)$", output, re.M)
    if m and "total" in m.group(1):
        line = m.group(1)
        get = lambda k: int(re.search(rf"(\d+) {k}", line).group(1)) if re.search(rf"(\d+) {k}", line) else 0
        return get("passed"), get("failed"), get("skipped") + get("todo")
    # node --test (TAP summary)
    if re.search(r"^# pass \d+", output, re.M):
        get = lambda k: int(re.search(rf"^# {k} (\d+)", output, re.M).group(1)) if re.search(rf"^# {k} (\d+)", output, re.M) else 0
        return get("pass"), get("fail"), get("skipped") + get("todo")
    # cargo
    ms = re.findall(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", output)
    if ms:
        return (sum(int(a) for a, _, _ in ms), sum(int(b) for _, b, _ in ms),
                sum(int(c) for _, _, c in ms))
    # JUnit XML (Gradle, Maven, many others)
    if project is not None:
        r = parse_junit_dir(Path(project), since=since)
        if r is not None:
            return r
    return None, None, None


def parse_junit_dir(project: Path, since: Optional[float] = None):
    files = []
    for pattern in ("**/build/test-results/**/*.xml", "**/target/surefire-reports/*.xml",
                    "**/test-results/**/*.xml"):
        files.extend(project.glob(pattern))
    seen, passed, failed, skipped, found = set(), 0, 0, 0, False
    for f in files:
        if f in seen or "node_modules" in f.parts:
            continue
        seen.add(f)
        if since is not None and f.stat().st_mtime < since - 1:
            continue
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
        for s in suites:
            t = int(s.get("tests", 0))
            fl = int(s.get("failures", 0)) + int(s.get("errors", 0))
            sk = int(s.get("skipped", 0))
            passed += t - fl - sk
            failed += fl
            skipped += sk
            found = True
    return (passed, failed, skipped) if found else None


_ERR_RE = re.compile(r"(error|exception|traceback|failed|failure|assert|FAIL)", re.I)


def first_error_block(output: str, lines: int = 25) -> str:
    rows = output.splitlines()
    for i, row in enumerate(rows):
        if _ERR_RE.search(row):
            start = max(0, i - 3)
            return "\n".join(rows[start:start + lines])
    return "\n".join(rows[-lines:])


def tail(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


# ---------------------------------------------------------------- running

Wrap = Callable[[Path, str], list]


def default_wrap(project: Path, cmd: str) -> list:
    """Fallback: run through bash. The runner should pass a jail wrapper."""
    return ["bash", "-lc", cmd]


def jail_wrap(jail_script: str) -> Wrap:
    def _wrap(project: Path, cmd: str) -> list:
        return [jail_script, "--project", str(project), "--", "bash", "-lc", cmd]
    return _wrap


def run_check(project: Path, check: Check, out_dir: Path,
              wrap: Wrap = default_wrap, env: Optional[dict] = None) -> CheckResult:
    project, out_dir = Path(project), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", check.name)
    out_file = out_dir / f"check-{safe}.log"
    argv = wrap(project, check.cmd)
    t0 = time.time()
    env = dict(os.environ if env is None else env)
    # fresh bytecode cache per run: a same-size edit within one second must not run stale .pyc
    env.setdefault("PYTHONPYCACHEPREFIX", str(out_dir / f".pycache-{int(t0 * 1000)}"))
    timed_out = False
    with open(out_file, "wb") as fh:
        fh.write(f"$ {check.cmd}\n".encode())
        fh.flush()
        try:
            proc = subprocess.Popen(argv, cwd=str(project), stdout=fh, stderr=subprocess.STDOUT,
                                    env=env, start_new_session=True)
            try:
                code = proc.wait(timeout=check.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
                code = None
        except FileNotFoundError as e:
            fh.write(f"\n[could not start: {e}]\n".encode())
            code = 127
    secs = time.time() - t0
    output = out_file.read_text(encoding="utf-8", errors="replace")
    import shutil
    shutil.rmtree(env["PYTHONPYCACHEPREFIX"], ignore_errors=True)
    if timed_out:
        with open(out_file, "a", encoding="utf-8") as fh:
            fh.write(f"\n[stopped: took longer than {check.timeout_s}s]\n")
    p, f, s = parse_counts(output, project, since=t0)
    res = CheckResult(check.name, check.cmd, code, p, f, s, round(secs, 1), str(out_file), timed_out)
    if not res.ok:
        res.first_error = first_error_block(output)
    return res


def _kill_group(proc: subprocess.Popen) -> None:
    import signal
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def run_all(project: Path, out_dir: Path, wrap: Wrap = default_wrap,
            checks: Optional[list[Check]] = None,
            extra_runners: Optional[dict] = None) -> dict:
    """Run every check. Returns a dict for the job record:
    {"status": passed|failed|unverified, "source": ..., "results": [...]}.
    extra_runners maps a check type (e.g. "vision") to a callable(project, check, out_dir).
    """
    source = "given"
    if checks is None:
        checks, source = load_checks(project)
    results = []
    for c in checks:
        if c.type != "command":
            runner = (extra_runners or {}).get(c.type)
            if runner is None:
                continue
            results.append(runner(project, c, out_dir))
            continue
        results.append(run_check(project, c, out_dir, wrap))
    return summarize(results, source)


def summarize(results: list, source: str) -> dict:
    dicts = [r.to_dict() if hasattr(r, "to_dict") else dict(r) for r in results]
    required = [d for d in dicts if d.get("mode", "required") != "advisory"]
    if not required:
        status = "unverified"
    elif all(d["ok"] for d in required):
        status = "passed"
    else:
        status = "failed"
    return {"status": status, "source": source, "results": dicts, "time": time.time()}


def banner_text(summary: dict) -> tuple[str, str]:
    """(colour, text) for the finished banner."""
    st = summary.get("status")
    res = summary.get("results", [])
    if st == "passed":
        total = sum((r.get("passed") or 0) for r in res)
        return "green", f"Checks passed ({total} tests)" if total else "Checks passed"
    if st == "failed":
        bad = [r for r in res if not r.get("ok") and r.get("mode", "required") != "advisory"]
        parts = []
        for r in bad:
            if r.get("timed_out"):
                parts.append(f"{r['name']} (took too long)")
            elif r.get("failed"):
                parts.append(f"{r['name']} ({r['failed']} failed)")
            else:
                parts.append(r["name"])
        return "red", "Checks failed: " + ", ".join(parts)
    if st == "needs_review":
        return "orange", "Needs review: " + summary.get("reason", "tests were changed")
    return "yellow", "Done – not verified (no build or tests found)"


def counts_total(summary: dict) -> tuple[int, int, bool]:
    """(tests passing, tests failing, build ok) used for 'better/worse' comparisons."""
    res = summary.get("results", [])
    passed = sum((r.get("passed") or 0) for r in res)
    failed = sum((r.get("failed") or 0) for r in res)
    build_ok = all(r.get("ok") for r in res if r.get("name") == "build") if res else False
    return passed, failed, build_ok
