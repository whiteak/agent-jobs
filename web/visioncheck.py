#!/usr/bin/env python3
"""Upgrade 6a (# U06): vision checks - screenshot the app, then ask the local model whether
it looks like the spec.

A check in AGENT_CHECKS.json:

  {"name": "looks right", "type": "vision", "target": "web", "open": "index.html",
   "steps": [{"click": "#start"}, {"key": " "}, {"wait": 500}],
   "spec": "a red orb, a score in the top right and a Start button",
   "size": [1280, 800], "mode": "advisory"}

  * open  - a file inside the project (served by a throw-away http server) or a URL
  * steps - optional clicks / keys / waits before the screenshot (see visionshot.py)
  * spec  - what the screen must show; the model goes through it point by point
  * mode  - "advisory" (default, shown but never fails the job) or "required"
  * votes - how often the model is asked (default 2; both must pass, see vision.check)

The screenshots are taken in the SAME jail the agent uses (checks.make_wrap ->
jail/run-local.sh sandbox-run, no network, only the project folder mounted), the driver
goes over as a here-doc and the PNGs come back on stdout, so nothing is written inside
the project folder. The model call is made by the board, after the agent has stopped.

The verdict itself lives in the tested engine (agentjobs_upgrades.vision); this module is
the glue: PNG sizing, the jail command, saving the shots + their log, the check result the
gate sums up, and the page/bundle bits.
"""
from __future__ import annotations

import base64
import functools
import importlib.util
import json
import os
import re
import struct
import subprocess
import time
import zlib
from pathlib import Path
from typing import Callable, Optional

from agentjobs_upgrades import vision
import badges

WEB = Path(__file__).resolve().parent
MAX_WIDTH = 1280                    # the model gets at most this many pixels across
DEFAULT_SIZE = (1280, 800)
MODEL_URL = os.environ.get("JOBBOARD_MODEL_URL") or "http://127.0.0.1:8080/v1/chat/completions"
STEP_KEYS = ("click", "type", "fill", "key", "wait", "wait_for", "shot")
STEP_ARGS = ("text",)                         # {"type": "#name", "text": "hello"}
STEP_MAX = 40
DRIVER_EOF = "__VISIONSHOT_DRIVER_EOF__"
OUT_MARK = "VISIONSHOT_JSON"
WORD = {"pass": "looks right", "fail": "does not look right", "inconclusive": "not sure"}
COLOUR = {"pass": "green", "fail": "red", "inconclusive": "yellow"}
NO_BROWSER = ("Screenshots need a browser: this machine has no Playwright and no Chromium. "
              "Ask Jason to add one to the sandbox image "
              "(python3 -m playwright install chromium). This is not a problem with the code - "
              "do not try to install anything.")


def tail(text, n=800):
    rows = str(text or "").splitlines()
    return "\n".join(rows[-n:])


# ---------------------------------------------------------------- PNG sizing (stdlib only)

def png_size(png: bytes):
    """(width, height) of a PNG, or None if it is not one."""
    if not png or not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    for typ, data in _chunks(png):
        if typ == b"IHDR":
            return struct.unpack(">II", data[:8])
    return None


def _chunks(png: bytes):
    i = 8
    while i + 8 <= len(png):
        n = struct.unpack(">I", png[i:i + 4])[0]
        typ = png[i + 4:i + 8]
        yield typ, png[i + 8:i + 8 + n]
        i += 12 + n
        if typ == b"IEND":
            return


_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}          # PNG colour type -> bytes per pixel


def _png_decode(png: bytes):
    """(w, h, bytes_per_pixel, rows) with the PNG filters undone, or None. Only plain
    8-bit non-interlaced PNGs - which is exactly what Playwright writes."""
    ihdr, idat = None, b""
    for typ, data in _chunks(png):
        if typ == b"IHDR":
            ihdr = data
        elif typ == b"IDAT":
            idat += data
        elif typ == b"IEND":
            break
    if not ihdr or not idat:
        return None
    try:
        w, h, depth, ctype, _comp, _filt, inter = struct.unpack(">IIBBBBB", ihdr[:13])
    except struct.error:
        return None
    if depth != 8 or ctype not in _CHANNELS or inter != 0 or w <= 0 or h <= 0:
        return None
    ch = _CHANNELS[ctype]
    stride = w * ch
    try:
        raw = zlib.decompress(idat)
    except zlib.error:
        return None
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(h):
        if i + 1 + stride > len(raw):
            return None
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        if f == 1:
            for x in range(ch, stride):
                line[x] = (line[x] + line[x - ch]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                b = prev[x]
                c = prev[x - ch] if x >= ch else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        elif f != 0:
            return None
        rows.append(line)
        prev = line
    return w, h, ch, rows


def _png_encode(w: int, h: int, ch: int, rows) -> bytes:
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}.get(ch, 6)

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def fit_width(png: bytes, max_w: int = MAX_WIDTH):
    """(png, note). The model takes at most max_w pixels across, so a wider screenshot is
    scaled here. Screenshots are captured at a viewport that is already <= max_w wide, so
    this only ever kicks in for an oversized one; an undecodable PNG is passed through with
    a note instead of failing the check."""
    size = png_size(png)
    if not size:
        return png, "the screenshot is not a readable PNG"
    w, h = size
    if w <= max_w:
        return png, ""
    dec = _png_decode(png)
    if dec is None:
        return png, "the screenshot was %dx%d and could not be resized here" % (w, h)
    dw, dh, ch, rows = dec
    new_h = max(1, round(dh * max_w / dw))
    out = []
    for y in range(new_h):
        src = rows[min(dh - 1, y * dh // new_h)]
        line = bytearray()
        for x in range(max_w):
            px = min(dw - 1, x * dw // max_w) * ch
            line += src[px:px + ch]
        out.append(line)
    return _png_encode(max_w, new_h, ch, out), "resized from %dx%d to %dx%d" % (w, h, max_w, new_h)


# ---------------------------------------------------------------- what to capture

def spec_for(check) -> dict:
    """The check entry -> what the driver is told to do."""
    extra = check.extra or {}
    w, h = DEFAULT_SIZE
    size = extra.get("size")
    if isinstance(size, (list, tuple)) and len(size) == 2:
        try:
            w, h = int(size[0]), int(size[1])
        except (TypeError, ValueError):
            w, h = DEFAULT_SIZE
    w = max(320, min(MAX_WIDTH, w))          # never ask the browser for more than the model can take
    h = max(240, min(4096, h))
    steps = []
    for s in (extra.get("steps") or [])[:STEP_MAX]:
        if isinstance(s, dict) and any(k in s for k in STEP_KEYS):
            steps.append({k: (str(v)[:200] if k == "text" else v) for k, v in s.items()
                          if k in STEP_KEYS or k in STEP_ARGS})
    return {"open": str(extra.get("open") or "")[:300], "size": [w, h], "steps": steps,
            "step_timeout_ms": 10000}


def spec_text(check) -> str:
    return str((check.extra or {}).get("spec") or "").strip()


# ---------------------------------------------------------------- the check entry

SPEC_MAX = 4000
OPEN_MAX = 300
ENTRY_KEYS = {"name", "cmd", "timeout_s", "type", "target", "open", "steps", "spec", "size",
              "mode", "votes"}
ENTRY_MAX = 12000


def _word(v, maxlen, required=False):
    return isinstance(v, str) and len(v) <= maxlen and (v.strip() or not required)


def check_ok(entry) -> bool:
    """Is a vision check entry well-formed? It comes from the project's AGENT_CHECKS.json,
    so it is validated before it is frozen into the job record: bounded text, a known set
    of keys, and both something to open and something to look for."""
    if not isinstance(entry, dict) or len(json.dumps(entry, default=str)) > ENTRY_MAX:
        return False
    if set(entry) - ENTRY_KEYS:
        return False
    if not _word(entry.get("spec"), SPEC_MAX, True) or not _word(entry.get("open"), OPEN_MAX, True):
        return False
    if entry.get("target", "web") != "web":
        return False
    if entry.get("mode", "advisory") not in ("advisory", "required"):
        return False
    size = entry.get("size", list(DEFAULT_SIZE))
    if not (isinstance(size, list) and len(size) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 4096 for v in size)):
        return False
    votes = entry.get("votes", 2)
    if not (isinstance(votes, int) and not isinstance(votes, bool) and 1 <= votes <= 5):
        return False
    steps = entry.get("steps", [])
    if not isinstance(steps, list) or len(steps) > STEP_MAX:
        return False
    for s in steps:
        if not isinstance(s, dict) or not any(k in s for k in STEP_KEYS):
            return False
        for k, v in s.items():
            if k not in STEP_KEYS and k not in STEP_ARGS:
                return False
            if not isinstance(v, str) and not (isinstance(v, int) and not isinstance(v, bool)):
                return False
            if isinstance(v, str) and len(v) > 300:
                return False
    return True


def frozen(check):
    """A checks.Check of type "vision" -> the dict frozen into the job record.
    None when the entry cannot be used."""
    extra = check.extra or {}
    entry = {"name": str(check.name)[:80], "type": "vision", "timeout_s": int(check.timeout_s),
             "target": "web", "open": str(extra.get("open") or ""), "spec": str(extra.get("spec") or "")}
    for k in ("steps", "size", "mode", "votes"):
        if k in extra:
            entry[k] = extra[k]
    return entry if check_ok(entry) else None


# ---------------------------------------------------------------- the screenshot

@functools.lru_cache(maxsize=None)
def _py_playwright() -> bool:
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


@functools.lru_cache(maxsize=None)
def _node_playwright() -> bool:
    try:
        return subprocess.run(["node", "-e", "process.stdout.write(require.resolve('playwright'))"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def browsers_installed() -> bool:
    """Is a Playwright browser (Chromium) present in the cache it looks in?"""
    roots = [os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "", str(Path.home() / ".cache" / "ms-playwright")]
    for r in roots:
        if not r:
            continue
        try:
            if any(p.name.startswith("chromium") and p.is_dir() for p in Path(r).iterdir()):
                return True
        except OSError:
            continue
    return False


def backend(force: str = ""):
    """('python' | 'node' | '', why) - what can take a screenshot here."""
    if force in ("python", "node"):
        return force, ""
    if not (_py_playwright() or _node_playwright()):
        return "", NO_BROWSER
    if not browsers_installed():
        return "", ("Playwright is there but its browser is not: run "
                    "`python3 -m playwright install chromium` in the sandbox image.")
    return ("python" if _py_playwright() else "node"), ""


def driver_source(kind: str) -> str:
    return (WEB / ("visionshot.py" if kind == "python" else "visionshot.mjs")).read_text(encoding="utf-8")


def capture_command(kind: str, spec: dict) -> str:
    """The shell command that runs the driver. The driver text and its spec go with it
    (here-doc + base64 env var) because the sandbox only mounts the project folder."""
    interp = "python3 -" if kind == "python" else "node --input-type=module"
    b64 = base64.b64encode(json.dumps(spec).encode()).decode()
    return "VISION_SPEC_B64=%s %s <<'%s'\n%s\n%s\n" % (b64, interp, DRIVER_EOF, driver_source(kind), DRIVER_EOF)


def default_run(argv, cwd, timeout):
    """(exit code, stdout, stderr)."""
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as ex:
        return 124, ex.stdout or b"", b"[stopped: the screenshot took too long]\n" + (ex.stderr or b"")
    except OSError as ex:
        return 127, b"", str(ex).encode()


def parse_envelope(stdout: bytes):
    """The driver's answer line (VISIONSHOT_JSON <base64>), or None."""
    mark = OUT_MARK.encode() + b" "
    for line in reversed((stdout or b"").splitlines()):
        line = line.strip()
        if line.startswith(mark):
            try:
                obj = json.loads(base64.b64decode(line[len(mark):]))
            except (ValueError, TypeError):
                return None
            return obj if isinstance(obj, dict) else None
    return None


def capture(project, spec: dict, wrap: Callable = None, kind: str = "",
            run: Callable = default_run, timeout: int = 600) -> dict:
    """Screenshots of the project, taken through `wrap` (the jail). Returns
    {shots: [(name, png)], url, steps, log, error} - never raises."""
    kind, why = backend(kind)
    if not kind:
        return {"shots": [], "url": "", "steps": [], "log": "", "error": why}
    argv = wrap(project, capture_command(kind, spec)) if wrap else ["bash", "-lc", capture_command(kind, spec)]
    rc, out, err = run(argv, project, timeout)
    err = (err or b"").decode("utf-8", "replace")
    env = parse_envelope(out)
    if env is None:
        return {"shots": [], "url": "", "steps": [], "log": tail(err),
                "error": "the screenshot program did not answer (exit %s)" % rc}
    shots = []
    for s in env.get("shots") or []:
        if not isinstance(s, dict):
            continue
        try:
            shots.append((str(s.get("name") or "shot")[:40], base64.b64decode(s.get("b64") or "")))
        except (ValueError, TypeError):
            continue
    return {"shots": shots, "url": str(env.get("url") or ""), "steps": env.get("steps") or [],
            "log": tail(err), "error": str(env.get("error")) if env.get("error") else None}


# ---------------------------------------------------------------- the check

def run_check(project, check, out_dir, *, wrap: Callable = None, model_url: str = "",
              emit: Callable = None, capture_fn: Callable = None, call: Callable = None,
              votes: int = None, budget: int = None) -> dict:
    """One vision check, in the shape checks.run_all wants (extra_runners entry).
    `budget` is the wall-clock ceiling; by default it is the check's own timeout_s, so a
    slow browser or a model that never answers cannot stall the gate forever."""
    t0 = time.time()
    budget = int(budget) if budget is not None else max(60, int(check.timeout_s or 600))
    deadline = t0 + budget
    shot_timeout = max(30, min(budget, 600))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    extra = check.extra or {}
    mode = extra.get("mode") if extra.get("mode") in ("advisory", "required") else "advisory"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", check.name)
    log_file = out_dir / ("check-%s.log" % safe)
    lines = ["vision check: %s (mode: %s)" % (check.name, mode),
             "open: %s" % (extra.get("open") or "-"), "", "spec:", spec_text(check) or "(none)", ""]
    shots, final_png = [], None
    asked = {}

    def finish(verdict, cap=None):
        res = vision.to_check_result(check.name, verdict, mode, screenshots=[p for _, p in shots])
        res.update({"cmd": ("vision: %s" % (extra.get("open") or ""))[:200],
                    "seconds": round(time.time() - t0, 1), "output_file": str(log_file),
                    "timed_out": False, "url": (cap or {}).get("url") or ""})
        if asked.get("n"):
            res["asked"] = asked["n"]
        log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if emit:
            emit("checks", 'Screen check "%s": %s' % (check.name, WORD.get(verdict["verdict"], verdict["verdict"])),
                 {"colour": COLOUR.get(verdict["verdict"], "yellow")})
        return res

    if not spec_text(check):
        return finish({"verdict": "inconclusive", "issues": ["This vision check has no spec text "
                                                            "to compare the screen with."]})

    cap = (capture_fn or (lambda pr, sp: capture(pr, sp, wrap=wrap, timeout=shot_timeout)))(project, spec_for(check))
    for st in cap.get("steps") or []:
        lines.append("step %s: %s%s" % (st.get("step"), st.get("what"),
                                        " -> " + str(st.get("error")) if st.get("error") else ""))
    if cap.get("url"):
        lines.append("page: " + cap["url"])
    if cap.get("log"):
        lines += ["", "browser output:", cap["log"]]
    if cap.get("error"):
        lines.append("capture error: " + str(cap["error"]))
    for i, (sname, png) in enumerate(cap.get("shots") or []):
        if png_size(png) is None:
            lines.append("note: screenshot %d (%s) is not a PNG - left out" % (i + 1, sname))
            continue
        png, note = fit_width(png)
        if note:
            lines.append("note: " + note)
        p = out_dir / ("vision-%s-%d-%s.png" % (safe, i + 1, re.sub(r"[^A-Za-z0-9_.-]+", "_", sname)))
        try:
            p.write_bytes(png)
        except OSError:
            continue
        shots.append((sname, str(p)))
        size = png_size(png) or (0, 0)
        lines.append("screenshot: %s (%dx%d)" % (p.name, size[0], size[1]))
        final_png = png
    if final_png is None:
        issues = [str(cap.get("error") or "No screenshot could be taken.")]
        return finish({"verdict": "inconclusive", "issues": issues}, cap)

    raws, errors = [], []
    ask = call or vision.http_call(model_url or MODEL_URL, timeout=min(300, max(30, budget)))

    def ask_and_record(body):
        if time.time() > deadline:                      # no more votes: the gate must not stall
            errors.append("ran out of time while looking at the screen")
            return ""
        try:
            answer = ask(body)
        except Exception as ex:                         # a dead or image-blind server must not break the gate
            errors.append(str(ex)[:300])
            return ""
        raws.append(answer)
        return answer

    n = votes if isinstance(votes, int) else extra.get("votes")
    n = n if isinstance(n, int) and not isinstance(n, bool) else 2
    n = max(1, min(5, n))
    asked["n"] = n
    verdict = vision.check(final_png, spec_text(check), ask_and_record, votes=n)
    lines += ["", "asked the model %d time(s)" % n]
    for i, a in enumerate(raws):
        lines += ["--- answer %d ---" % (i + 1), a[:4000]]
    for e in errors:
        lines.append("model call failed: " + e)
    if errors:
        verdict = dict(verdict, issues=verdict["issues"] +
                       ["The model server did not answer (%s)." % errors[0]])
    lines += ["", "verdict: %s" % verdict["verdict"]] + (["issues:"] + ["- " + i for i in verdict["issues"]])
    return finish(verdict, cap)


def runners(wrap: Callable = None, model_url: str = "", emit: Callable = None,
            capture_fn: Callable = None, call: Callable = None) -> dict:
    """The extra_runners dict for checks.run_all."""
    return {"vision": lambda project, check, out_dir: run_check(
        project, check, out_dir, wrap=wrap, model_url=model_url, emit=emit,
        capture_fn=capture_fn, call=call)}


# ---------------------------------------------------------------- showing the shots

def shot_file(m, src, n, i, job_root):
    """One screenshot's file, only if it is inside JOBS/<job id>/ (same rule as
    badges.check_text). None = 404."""
    res = badges.results(m, src)
    if res is None:
        return None
    try:
        item = res[int(n)]
        paths = item.get("screenshots") or []
        p = Path(str(paths[int(i)]))
    except (IndexError, ValueError, TypeError, AttributeError, KeyError):
        return None
    if not str(p):
        return None
    try:
        p = p.resolve()
        root = Path(job_root).resolve()
    except OSError:
        return None
    if not p.is_file() or p.suffix.lower() != ".png" or root not in p.parents:
        return None
    return p
