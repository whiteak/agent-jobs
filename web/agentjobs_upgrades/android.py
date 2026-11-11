"""Upgrade 6b: drive an Android device with adb - install the debug APK, start it, screenshot it.

The board's screen check (6a) needs a picture; for an Android app the picture comes from a
device. Everything here is a plain function over one `Adb` object that turns an argv list
into (exit code, stdout bytes, stderr text): the board injects the real subprocess call and
the tests put a fake adb script on PATH. No shell is used on THIS machine, so a path or a
step that came out of a project file can only ever be one argument of a fixed command.
`adb shell` is the one exception: adb joins its words with spaces and the phone runs them
through /system/bin/sh, so everything that goes through `Adb.shell` has to be text that
shell would take literally - see SHELL_META.

Why adb runs on the host and not inside the job jail: jail/podman-local.sh starts every job
container with `--network none` and no USB, so a container has no way to reach a device.
The project's own code still never runs on the host - it is installed and started ON the
device - and nothing is written inside the project folder: the screenshots come back as
bytes, the same way the 6a browser driver returns PNGs on stdout.

The two sequences the board uses:

  capture(project, spec)  install, start, wait for a still screen, screenshot, steps (with a
                          screenshot after each) - answers in the shape 6a's capture_fn uses
  smoke(project, spec)    install, start, hold for hold_s seconds, no crash - no model needed
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

DEFAULT_TIMEOUT = 120                   # one adb command
INSTALL_TIMEOUT = 300
SCREENSHOT_TIMEOUT = 60
LOG_TIMEOUT = 30
STABLE_GAP_S = 1.0                      # two shots this far apart must match
STABLE_MAX_S = 20.0                     # ... at most this long
HOLD_S = 10                             # android-smoke: how long the app must survive
MAX_STEPS = 40
MAX_LOG_LINES = 400
CRASH_LINES = 12                        # logcat lines kept in the result
MAX_CRASH_LINES = 24                    # logcat lines read from the device
APK_GLOBS = ("app/build/outputs/apk/debug/*.apk", "*/build/outputs/apk/debug/*.apk",
             "build/outputs/apk/debug/*.apk")
CRASH_MARKS = ("FATAL EXCEPTION", "Fatal signal", "ANR in", "beginning of crash", "has died")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
SDK_ADBS = ("/opt/android-sdk/platform-tools", str(Path.home() / "agent-sandbox/state/android-sdk/platform-tools"))

NO_ADB = ("This machine has no adb (the Android SDK platform-tools), so the app cannot be "
          "started on a device. This is not a problem with the code.")
NO_DEVICE = "No device connected - plug in a phone or start an emulator."
NO_APK = "APK not built - there is no debug APK under app/build/outputs/apk/debug."
NOT_FILLED = "the device was never chosen for this check"

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,120}(\.[A-Za-z][A-Za-z0-9_]{1,120}){1,10}$")
COMP_RE = re.compile(r"^[A-Za-z0-9_.]{1,260}/[A-Za-z0-9_.]{1,260}$")
KEY_RE = re.compile(r"^(KEYCODE_[A-Z0-9_]{1,30}|[0-9]{1,3})$")

# What `adb shell` really does: adb joins its arguments with spaces and hands the result to
# /system/bin/sh ON THE PHONE, so a word holding one of these would be run on the device
# (`{"text": "a; pm uninstall x"}` or `{"text": "$(reboot)"}` are commands, not typing).
# SHELL_META is the list the review asked for; TEXT_RE is what is left of plain printable
# ASCII once they are gone, so a step's text can only ever be typed into a field.
SHELL_META = ";|&$`()<>\"'\\*?~#!{}[]" + "\n\r\t"
SHELL_META_RE = re.compile("[" + re.escape(SHELL_META) + "]")
TEXT_RE = re.compile(r"^[A-Za-z0-9 %,.:/=+@^_-]{1,200}$")


def find_adb(extra: str = "") -> str:
    """The adb to use: the setting, then PATH, then the SDK folders this sandbox uses."""
    for cand in (extra, os.environ.get("ANDROID_ADB", ""), shutil.which("adb") or "",
                 *(str(Path(d) / "adb") for d in SDK_ADBS)):
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return ""


def default_run(argv, timeout: float = DEFAULT_TIMEOUT):
    """(exit code, stdout bytes, stderr text). argv only - never a shell."""
    try:
        p = subprocess.run(list(argv), capture_output=True, timeout=max(1, timeout))
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as ex:
        return 124, ex.stdout or b"", b"[stopped: the device took too long to answer]\n" + (ex.stderr or b"")
    except OSError as ex:
        return 127, b"", str(ex).encode()


def _both(out, err) -> str:
    """What the device said, both streams as one text."""
    return (out or b"").decode("utf-8", "replace") + "\n" + (err or "")


class Adb:
    """One device, one command at a time. `run`/`sleep`/`now` are injectable so the tests
    never wait a second for a screenshot pair."""

    def __init__(self, serial: str = "", path=None, run: Callable = None, sleep=None,
                 now=None, deadline: float = None):
        self.serial = str(serial or "")
        self.path = find_adb() if path is None else str(path or "")
        self.run = run or default_run
        self.sleep = sleep or time.sleep
        self.now = now or time.monotonic
        self.deadline = deadline
        self.log = []

    def note(self, line: str) -> None:
        if len(self.log) < MAX_LOG_LINES:
            self.log.append(str(line)[:400])

    def call(self, args, timeout: float = DEFAULT_TIMEOUT, quiet: bool = False):
        """(exit code, stdout bytes, stderr text) for one adb command."""
        words = [str(a) for a in args]
        argv = [self.path or "adb"] + (["-s", self.serial] if self.serial else []) + words
        left = timeout
        if self.deadline is not None:
            left = max(1, min(timeout, int(self.deadline - self.now())))
        self.note("$ " + " ".join(words))
        if not self.path:
            self.note("(no adb on this machine)")
            return 127, b"", NO_ADB.encode()
        rc, out, err = self.run(argv, left)
        if quiet:
            self.note("< %s %d bytes" % ("ok" if rc == 0 else "exit %s" % rc, len(out or b"")))
        else:
            for line in ((out or b"") + b"\n" + (err or b"")).decode("utf-8", "replace").splitlines():
                if line.strip():
                    self.note(line)
        return rc, out or b"", (err or b"").decode("utf-8", "replace")

    def shell(self, *words, **kw):
        """`adb shell ...`. adb joins these words with spaces and the PHONE's /system/bin/sh
        parses what comes out, so a word holding `;` `|` `$` a quote or a backtick is a
        command on the device, not an argument. Every caller validates what it passes; this
        is the last line of defence: the command is refused, never sent."""
        for w in words:
            if SHELL_META_RE.search(str(w)):
                bad = str(w)[:80]
                self.note('(refused: "%s" holds a character the phone\'s shell would run)' % bad)
                return 126, b"", ("refused: %s holds a character the phone's shell would run\n" % bad)
        return self.call(["shell"] + list(words), **kw)

    def time_left(self) -> float:
        return 1e9 if self.deadline is None else self.deadline - self.now()


# ---------------------------------------------------------------- the device

def parse_devices(text) -> list:
    """`adb devices` output -> the serials that are ready to use."""
    out = []
    for line in (text or "").splitlines() if isinstance(text, str) else []:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def devices(adb: Adb) -> list:
    rc, out, err = adb.call(["devices"], timeout=LOG_TIMEOUT)
    if rc != 0:
        return []
    return parse_devices(_both(out, err))


def pick_device(adb: Adb, want: str = ""):
    """(serial, why empty). `want` is the device named by the check or the settings; empty
    means the first connected one. A `{{ANDROID_DEVICE}}` nobody filled in counts as empty."""
    if not adb.path:
        return "", NO_ADB
    want = str(want or "").strip()
    if "{" in want or "}" in want:                 # a {{ANDROID_DEVICE}} nobody filled in
        want = ""
    found = devices(adb)
    if want:
        if want in found:
            return want, ""
        return "", ("Device %s is not connected (adb devices shows: %s)"
                    % (want[:60], ", ".join(found) if found else "no device"))
    if found:
        return found[0], ""
    return "", NO_DEVICE


# ---------------------------------------------------------------- the APK

def find_apk(project, rel: str = ""):
    """(Path or None, why). The APK is always a file inside the project folder - a check
    cannot name a path outside it, and it is only ever handed to adb as one argument."""
    root = Path(project)
    try:
        root = root.resolve()
    except OSError:
        return None, NO_APK
    if rel:
        rel = str(rel).strip()
        cand = None
        if not Path(rel).is_absolute():
            try:
                cand = (root / rel).resolve()
            except OSError:
                cand = None
        if cand is None or not cand.is_file() or cand.suffix.lower() != ".apk" \
                or root not in cand.parents:
            return None, "APK not built: %s is not a file inside the project folder" % rel[:120]
        return cand, ""
    found = []
    for pattern in APK_GLOBS:
        for p in root.glob(pattern):
            if p.suffix.lower() == ".apk" and p.is_file() and "node_modules" not in p.parts:
                found.append(p)
    if not found:
        return None, NO_APK
    return max(found, key=lambda p: (p.stat().st_mtime, str(p))), ""


# ---------------------------------------------------------------- install and start

def package_list(adb: Adb) -> set:
    """Third-party packages on the device (the ones an install can add). What the device
    reports goes back into `adb shell` later (force-stop, pidof, resolve-activity), so a
    name that is not a package name is dropped here rather than sent back to the phone."""
    rc, out, err = adb.shell("pm", "list", "packages", "-3", timeout=LOG_TIMEOUT)
    if rc != 0:
        return set()
    text = (out or b"").decode("utf-8", "replace")
    return {name for name in (line.strip()[len("package:"):] for line in text.splitlines()
                              if line.strip().startswith("package:")) if PKG_RE.match(name)}


def gradle_app_id(project) -> str:
    """The applicationId in the build file, used when the device cannot say what was installed."""
    for name in ("app/build.gradle", "app/build.gradle.kts", "build.gradle"):
        f = Path(project) / name
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r'applicationId\s*=?\s*"([A-Za-z][A-Za-z0-9_.]{2,120})"', text)
        if m and PKG_RE.match(m.group(1)):
            return m.group(1)
    return ""


def package_for(adb: Adb, entry: dict, before: set, project) -> str:
    """Which app the APK just installed: what it says in the check, else the package that
    was not there before the install, else the applicationId in the build file."""
    want = str((entry or {}).get("pkg") or "").strip()
    if want and PKG_RE.match(want):
        return want
    new = sorted(package_list(adb) - (before or set()))
    if len(new) == 1:
        return new[0]
    gradle = gradle_app_id(project)
    if gradle:
        return gradle
    return new[0] if new else ""


def install(adb: Adb, apk) -> tuple:
    """(ok, text). `adb install -r` keeps the app's data between runs."""
    rc, out, err = adb.call(["install", "-r", str(apk)], timeout=INSTALL_TIMEOUT)
    text = _both(out, err).strip()
    ok = rc == 0 and "Failure" not in text and "failed" not in text.lower()
    return ok, text


def resolve_activity(adb: Adb, pkg: str) -> str:
    """The launcher component (`com.x/com.x.MainActivity`) of an installed app."""
    rc, out, err = adb.shell("cmd", "package", "resolve-activity", "--brief", pkg, timeout=LOG_TIMEOUT)
    if rc != 0:
        return ""
    lines = [l.strip() for l in _both(out, err).splitlines() if l.strip() and "warning" not in l.lower()]
    if not lines:
        return ""
    line = lines[-1]
    m = re.search(r"ComponentInfo\{([^}]+)\}", line)
    comp = (m.group(1) if m else line.split()[-1]).strip()
    return comp if COMP_RE.match(comp) else ""


def start(adb: Adb, comp: str) -> tuple:
    """(ok, text). `am start -W` waits for the activity to be drawn."""
    rc, out, err = adb.shell("am", "start", "-W", "-n", comp, timeout=INSTALL_TIMEOUT)
    text = _both(out, err).strip()
    ok = rc == 0 and "Error" not in text
    return ok, text


def pid(adb: Adb, pkg: str):
    """The app's process id, "" when it is not running, None when this device has no pidof."""
    rc, out, err = adb.shell("pidof", pkg, timeout=LOG_TIMEOUT, quiet=True)
    words = (out or b"").decode("utf-8", "replace").split()
    if words:
        return words[0]
    low = (err or "").lower()
    if rc != 0 and ("not found" in low or "inaccessible" in low or rc == 127):
        return None
    return ""


def crash_lines(adb: Adb, pkg: str = "", limit: int = 200) -> list:
    """The logcat blocks that say the app died, stack trace included (up to two of them).
    Only blocks naming the package count when a package is given."""
    rc, out, err = adb.shell("logcat", "-d", "-t", str(int(limit)), timeout=LOG_TIMEOUT)
    if rc != 0:
        return []
    lines = (out or b"").decode("utf-8", "replace").splitlines()
    marks = [i for i, l in enumerate(lines) if any(m in l for m in CRASH_MARKS)]
    if pkg:         # another app's crash is not this job's problem
        marks = [i for i in marks if any(pkg in lines[j] for j in range(i, min(len(lines), i + 4)))]
    keep = []
    for i in marks[:2]:
        keep += lines[i:i + CRASH_LINES]
    return [l.strip()[:300] for l in keep if l.strip()][:MAX_CRASH_LINES]


def app_crashed(adb: Adb, pkg: str) -> tuple:
    """(crashed, crash lines) - the process is gone, or logcat says it died."""
    alive = pid(adb, pkg)
    lines = crash_lines(adb, pkg)
    return (alive == "" or bool(lines)), lines


# ---------------------------------------------------------------- the screen

def screencap(adb: Adb):
    """`adb exec-out screencap -p` -> PNG bytes, or None."""
    rc, out, err = adb.call(["exec-out", "screencap", "-p"], timeout=SCREENSHOT_TIMEOUT, quiet=True)
    if rc != 0 or not (out or b"").startswith(PNG_MAGIC):
        return None
    return out


def wait_stable(adb: Adb, gap: float = STABLE_GAP_S, max_wait: float = STABLE_MAX_S):
    """Two screenshots `gap` apart that match means the app has finished drawing.
    Returns (png or None, stable, screenshots taken); it never waits longer than max_wait."""
    gap = max(0.2, min(5.0, float(gap or STABLE_GAP_S)))
    max_wait = max(gap, min(60.0, float(max_wait or STABLE_MAX_S)))
    first = screencap(adb)
    if first is None:
        return None, False, 0
    shots, stop = 1, adb.now() + max_wait
    if adb.deadline is not None and adb.now() >= adb.deadline:
        return first, False, shots                      # the check is out of time already
    while True:
        left = stop - adb.now()
        if left <= 0:
            return first, False, shots
        adb.sleep(min(gap, max(0.05, left)))
        second = screencap(adb)
        shots += 1
        if second is None:
            return first, False, shots
        if second == first:
            return second, True, shots
        first = second


def step_words(step):
    """A step -> the `input ...` words for the device, or (None, why). Wait steps answer
    (None, None) and are slept by the caller. Nothing here reaches a shell."""
    if not isinstance(step, dict):
        return None, "not a step"
    if "wait" in step:
        return None, None

    def coords(value, n):
        vals = value if isinstance(value, (list, tuple)) else str(value).split(",")
        if len(vals) != n:
            return None
        try:
            out = [int(v) for v in vals]
        except (TypeError, ValueError):
            return None
        return out if all(0 <= v <= 100000 for v in out) else None

    if "tap" in step:
        xy = coords(step["tap"], 2)
        return (["input", "tap"] + [str(v) for v in xy], "") if xy else (None, "tap needs two numbers")
    if "swipe" in step:
        xy = coords(step["swipe"], 4)
        return (["input", "swipe"] + [str(v) for v in xy], "") if xy else (None, "swipe needs four numbers")
    if "text" in step:
        text = str(step["text"] or "")
        if not TEXT_RE.match(text):
            return None, ("text may only be letters, numbers and plain punctuation - never a "
                          "character the phone's shell would run (; | & $ quotes ` ( ) < > "
                          "* ? # ! { } [ ]), up to 200 of them")
        return ["input", "text", text.replace(" ", "%s")], ""
    if "key" in step or "keyevent" in step:
        key = str(step.get("key") if "key" in step else step.get("keyevent")).strip()
        if not KEY_RE.match(key):
            return None, "key must be a key code (KEYCODE_...) or a number"
        return ["input", "keyevent", key], ""
    return None, "unknown step"


# ---------------------------------------------------------------- the two sequences

def _launch(project, spec, adb: Adb):
    """Install the APK and start its main activity.
    Returns {"apk","pkg","comp","error","fatal"} - error None when the app is up."""
    st = {"apk": None, "pkg": "", "comp": "", "error": None, "fatal": False}
    apk, why = find_apk(project, str((spec or {}).get("apk") or ""))
    if apk is None:
        st["error"], st["fatal"] = why, True
        return st
    st["apk"] = apk
    before = package_list(adb)
    ok, text = install(adb, apk)
    if not ok:
        st["error"] = "The app would not install: %s" % (text or "the device said no")[:600]
        st["fatal"] = True
        return st
    st["pkg"] = package_for(adb, spec, before, project)
    if not st["pkg"]:
        st["error"] = "The app was installed but the device would not say which app it is - " \
                      'put "pkg": "com.example.app" in the check.'
        st["fatal"] = True
        return st
    st["comp"] = resolve_activity(adb, st["pkg"])
    if not st["comp"]:
        st["error"] = "APK not built: %s has no launcher activity to open, so there is no " \
                      "screen to look at." % st["pkg"]
        st["fatal"] = True
        return st
    adb.shell("am", "force-stop", st["pkg"], timeout=LOG_TIMEOUT)
    adb.shell("logcat", "-c", timeout=LOG_TIMEOUT)
    ok, text = start(adb, st["comp"])
    if not ok:
        st["error"] = "The app did not start: %s" % (text or "the device refused")[:600]
        st["fatal"] = True
    return st


def _result(adb: Adb, st: dict, extra: dict):
    out = {"shots": [], "url": "", "steps": [], "log": "\n".join(adb.log), "error": None,
           "fatal": False, "device": adb.serial, "pkg": st.get("pkg") or "",
           "apk": str(st.get("apk") or ""), "crash": [], "stable": False}
    out.update(extra)
    return out


def capture(project, spec, adb_path=None, serial: str = "", run: Callable = None,
            sleep=None, now=None, deadline: float = None) -> dict:
    """The Android answer in 6a's capture shape: install, start, wait for a still screen,
    screenshot, then the optional steps with a screenshot after each. The picture named
    "final" is the last one, which is what 6a shows the model."""
    adb = Adb(serial, adb_path, run=run, sleep=sleep, now=now, deadline=deadline)
    spec = spec or {}
    st = _launch(project, spec, adb)
    if st["error"]:
        return _result(adb, st, {"error": st["error"], "fatal": st["fatal"]})

    png, stable, shots = wait_stable(adb, spec.get("stable_gap_s"), spec.get("stable_max_s"))
    if png is None:
        return _result(adb, st, {"error": "The device would not send a screenshot "
                                          "(screencap failed).", "fatal": True})
    adb.note("the screen held still after %d screenshots" % shots if stable
             else "the screen was still changing after %d screenshots - using the last one" % shots)
    died, crash = app_crashed(adb, st["pkg"])
    if died:
        return _result(adb, st, {"error": "App crashed on start - the screen never settled.",
                                 "fatal": True, "crash": crash, "stable": stable})

    shots_out = []
    notes = []
    last = png
    steps = (spec.get("steps") or [])[:MAX_STEPS]
    if not steps:
        shots_out.append(("final", png))
    else:
        shots_out.append(("start", png))
        for i, step in enumerate(steps):
            words, why = step_words(step)
            note = {"step": i, "what": json.dumps(step)[:200], "error": why}
            if why:
                adb.note("step %d: %s" % (i + 1, why))
            elif words is None:                                   # {"wait": 500}
                adb.sleep(max(0, min(60000, int(step.get("wait") or 0))) / 1000.0)
            else:
                rc, out, err = adb.shell(*words, timeout=LOG_TIMEOUT)
                note["error"] = None if rc == 0 else "the device refused this step (exit %s)" % rc
            notes.append(note)
            shot = screencap(adb)
            if shot:
                shots_out.append(("step%d" % (i + 1), shot))
                last = shot
        shots_out.append(("final", screencap(adb) or last))
    crash = crash_lines(adb, st["pkg"])
    return _result(adb, st, {"shots": shots_out, "stable": stable, "crash": crash,
                             "steps": notes})


def smoke(project, spec, adb_path=None, serial: str = "", run: Callable = None,
          sleep=None, now=None, deadline: float = None) -> dict:
    """Install, start, and the app must survive `hold_s` seconds. No screenshots, no model."""
    adb = Adb(serial, adb_path, run=run, sleep=sleep, now=now, deadline=deadline)
    st = _launch(project, spec or {}, adb)
    hold = (spec or {}).get("hold_s")
    hold = hold if isinstance(hold, int) and not isinstance(hold, bool) else HOLD_S
    hold = max(1, min(120, hold))
    if st["error"]:
        return _result(adb, st, {"error": st["error"], "fatal": st["fatal"]})
    if adb.time_left() <= 0:
        return _result(adb, st, {"error": "Ran out of time before the app could be checked.",
                                 "fatal": False})
    adb.sleep(min(hold, max(0.0, adb.time_left())))
    died, crash = app_crashed(adb, st["pkg"])
    if died:
        return _result(adb, st, {"error": "App crashed on start - it was not still running "
                                          "after %d seconds." % hold, "fatal": True, "crash": crash})
    return _result(adb, st, {})
