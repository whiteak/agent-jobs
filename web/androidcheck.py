#!/usr/bin/env python3
"""Upgrade 6b (# U06b): Android screen checks.

Two kinds of check, both written in the project's AGENT_CHECKS.json:

  {"name": "app runs", "type": "android-smoke", "mode": "required"}

  * installs app/build/outputs/apk/debug/*.apk (or the "apk" path from the check), starts
    the launcher activity and requires the app to still be running after 10 s. No model.
    This is what an Android project gets by default when it has no AGENT_CHECKS.json.

  {"name": "looks right", "type": "vision", "target": "android",
   "spec": "a list of notes and a + button bottom right",
   "steps": [{"tap": [360, 1900]}, {"text": "buy milk"}, {"key": "KEYCODE_ENTER"}],
   "device": "emulator-5554", "mode": "advisory"}

  * installs and starts the app, waits until the screen stops changing (two screenshots 1 s
    apart that match, at most 20 s), takes `adb exec-out screencap -p`, runs the optional
    steps with a screenshot after each, then asks the model the same two questions as 6a.

`device` is optional: the first connected device is used, or the serial in the board's
settings ("android_device"). A `{{ANDROID_DEVICE}}` nobody filled in counts as unset.

No device (or no adb) is never the app's fault: the check is reported as "skipped" and
forced to advisory, so it cannot fail a job. Everything else - no APK, an install that
failed, an app that crashed on start - is a real failure and goes to a fix round with the
logcat lines in it.

The device work itself is in the tested engine (agentjobs_upgrades.android); this module
is the glue: what a valid entry looks like, the frozen job record, which device to use,
the check result + log the gate wants, and the page/bundle bits.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

from agentjobs_upgrades import android, vision
import visioncheck   # U06: the model check, the PNG saving and the log are already there

SPEC_MAX = 4000
APK_MAX = 300
DEVICE_MAX = 80
STEP_MAX = 40
STEP_KEYS = ("tap", "swipe", "text", "key", "keyevent", "wait")
ENTRY_KEYS = {"name", "cmd", "timeout_s", "type", "target", "apk", "pkg", "device", "steps",
              "spec", "mode", "votes", "hold_s"}
ENTRY_MAX = 12000
CRASH_ISSUES = 8                        # crash lines shown as problems in the card/bundle/prompt
WORD = {"pass": "the app ran", "fail": "the app did not run", "skipped": "no device"}
COLOUR = {"pass": "green", "fail": "red", "skipped": "yellow"}
NO_RUNNER = "No Android check runner is set up for this job."


# ---------------------------------------------------------------- the check entry

def _word(v, maxlen, required=False):
    return isinstance(v, str) and len(v) <= maxlen and (v.strip() or not required)


def check_ok(entry) -> bool:
    """Is an Android check entry well-formed? It comes from the project's own
    AGENT_CHECKS.json, so it is checked before it is frozen into the job record: bounded
    text, a known set of keys, a package/device that cannot smuggle anything into adb, and
    steps that are only ever taps, swipes, text, keys or waits."""
    if not isinstance(entry, dict) or len(json.dumps(entry, default=str)) > ENTRY_MAX:
        return False
    if set(entry) - ENTRY_KEYS:
        return False
    kind = entry.get("type")
    if kind == "android-smoke":
        pass
    elif kind == "vision" and entry.get("target") == "android":
        if not _word(entry.get("spec"), SPEC_MAX, True):
            return False
    else:
        return False
    apk = entry.get("apk", "")
    if not _word(apk, APK_MAX) or Path(str(apk)).is_absolute() or ".." in Path(str(apk)).parts \
            or "\x00" in str(apk):
        return False
    pkg = entry.get("pkg", "")
    if not _word(pkg, 160) or (pkg and not android.PKG_RE.match(pkg.strip())):
        return False
    device = entry.get("device", "")
    if not _word(device, DEVICE_MAX) or (device.strip() and "{" not in device
                                         and not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", device.strip())):
        return False
    if entry.get("mode", "required" if kind == "android-smoke" else "advisory") not in ("advisory", "required"):
        return False
    hold = entry.get("hold_s", 10)
    if not (isinstance(hold, int) and not isinstance(hold, bool) and 1 <= hold <= 120):
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
            if k not in STEP_KEYS:
                return False
            if isinstance(v, str):
                if len(v) > 200 or "\x00" in v:
                    return False
                # U06b: a text step is typed on the phone through `adb shell input text`, so
                # a shell metacharacter in it is a command on the device. It never gets as
                # far as the job record.
                if k == "text" and not android.TEXT_RE.match(v):
                    return False
            elif isinstance(v, (list, tuple)):
                if len(v) != {"tap": 2, "swipe": 4}.get(k, len(v)) or not all(
                        isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 100000 for x in v):
                    return False
            elif not isinstance(v, int) or isinstance(v, bool):
                return False
    return True


def frozen(check) -> Optional[dict]:
    """An Android checks.Check -> the dict frozen into the job record, or None."""
    extra = check.extra or {}
    entry = {"name": str(check.name)[:80], "type": check.type, "timeout_s": int(check.timeout_s)}
    for k in ("target", "apk", "pkg", "device", "steps", "spec", "mode", "votes", "hold_s"):
        if k in extra:
            entry[k] = extra[k]
    if check.type == "vision":
        entry["target"] = "android"
    return entry if check_ok(entry) else None


def spec_for(check) -> dict:
    """The check entry -> what the engine is told to do."""
    extra = check.extra or {}
    spec = {"apk": str(extra.get("apk") or "")[:APK_MAX], "pkg": str(extra.get("pkg") or "").strip(),
            "steps": [s for s in (extra.get("steps") or [])[:STEP_MAX] if isinstance(s, dict)]}
    hold = extra.get("hold_s")
    if isinstance(hold, int) and not isinstance(hold, bool):
        spec["hold_s"] = max(1, min(120, hold))
    return spec


def mode_of(check, default: str = "advisory") -> str:
    mode = (check.extra or {}).get("mode")
    return mode if mode in ("advisory", "required") else default


def short_result(r) -> str:
    """Two or three words for the Checks card: what the device actually said."""
    for issue in (r.get("issues") or [])[:3]:
        text = str(issue)
        for start, words in (("App crashed", "crashed on start"), ("APK not built", "no APK built"),
                             ("The app would not install", "would not install"),
                             ("The app did not start", "would not start"),
                             ("No device", "no device"), ("No adb", "no adb"),
                             ("This machine has no adb", "no adb"),
                             ("Device ", "no device"),
                             ("Ran out of time", "ran out of time")):
            if text.startswith(start):
                return words
    return WORD.get(r.get("verdict") or "skipped", str(r.get("verdict") or "skipped"))


def device_want(check, cfg) -> str:
    """Which device the check asks for: the check itself, then the board's settings.
    A `{{ANDROID_DEVICE}}` nobody filled in is treated as unset."""
    want = str((check.extra or {}).get("device") or "").strip()
    if "{" in want or "}" in want:
        want = ""
    if not want:
        want = str((cfg or {}).get("android_device") or "").strip()
    return want[:DEVICE_MAX]


# ---------------------------------------------------------------- the result bits

def _log_file(out_dir, check) -> Path:
    return Path(out_dir) / ("check-%s.log" % re.sub(r"[^A-Za-z0-9_.-]+", "_", check.name))


def _skipped(check, out_dir, emit, why, t0, kind: str = "android") -> dict:
    """No adb / no device: say so plainly, and never fail the job over it."""
    log = _log_file(out_dir, check)
    log.write_text("%s check: %s\n\n%s\n" % (kind, check.name, why), encoding="utf-8")
    res = vision.to_check_result(check.name, {"verdict": "skipped", "issues": [why]}, "advisory")
    res.update({"cmd": "android: no device", "seconds": round(time.time() - t0, 1),
                "output_file": str(log), "timed_out": False, "url": "", "verdict": "skipped"})
    if emit:
        emit("checks", "%s check \"%s\": no device to check on" % (kind.title(), check.name),
             {"colour": "yellow"})
    return res


def _cap_state(cap, res) -> dict:
    """What the device did, so the card and the bundle can name the device and the crash."""
    for key in ("device", "pkg", "apk"):
        if cap.get(key):
            res[key] = cap[key]
    if cap.get("crash"):
        res["crash"] = list(cap["crash"])[:CRASH_ISSUES * 2]
    if cap:
        res["stable"] = bool(cap.get("stable"))
    return res


# ---------------------------------------------------------------- the checks

def run_check(project, check, out_dir, *, emit: Callable = None, cfg: dict = None,
              adb_path=None, model_url: str = "", call: Callable = None,
              capture_fn: Callable = None, budget: int = None) -> dict:
    """One Android screen check: the device work, then the same model check as 6a
    (visioncheck.run_check saves the PNGs, writes the log and asks the model)."""
    t0 = time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    budget = int(budget) if budget is not None else max(120, int(check.timeout_s or 600))
    deadline = t0 + budget
    adb_path = android.find_adb(str((cfg or {}).get("android_adb") or "")) if adb_path is None else adb_path
    holder = {}

    dev, why = android.pick_device(android.Adb("", adb_path, deadline=deadline), device_want(check, cfg))
    if not dev:
        return _skipped(check, out_dir, emit, why, t0)

    def capture(pr, spec):
        cap = (capture_fn or (lambda p, s: android.capture(p, s, serial=dev, adb_path=adb_path,
                                                           deadline=deadline)))(pr, spec)
        holder["cap"] = cap
        if cap.get("crash") and cap.get("fatal"):
            cap["issue_extra"] = list(cap["crash"])[:CRASH_ISSUES]
        return cap

    def say(kind, text, data=None):
        """The verdict line on the timeline - named after what the device actually said."""
        cap = holder.get("cap") or {}
        if emit:
            if cap.get("fatal") and cap.get("error"):
                text = 'Screen check "%s": %s' % (check.name, cap["error"])
            emit(kind, text, data)

    res = visioncheck.run_check(project, check, out_dir, capture_fn=capture, model_url=model_url,
                                emit=say, call=call, budget=budget, spec_fn=spec_for)
    res = _cap_state(holder.get("cap") or {}, res)
    cap = holder.get("cap") or {}
    res["cmd"] = "android: install + start %s" % (Path(str(cap["apk"])).name if cap.get("apk")
                                                  else "the debug APK")
    return res


def run_smoke(project, check, out_dir, *, emit: Callable = None, cfg: dict = None, adb_path=None,
              smoke_fn: Callable = None, budget: int = None) -> dict:
    """The android-smoke check: install, start, and the app must survive. No model needed."""
    t0 = time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mode = mode_of(check, "required")
    budget = int(budget) if budget is not None else max(120, int(check.timeout_s or 600))
    deadline = t0 + budget
    log = _log_file(out_dir, check)
    adb_path = android.find_adb(str((cfg or {}).get("android_adb") or "")) if adb_path is None else adb_path

    dev, why = android.pick_device(android.Adb("", adb_path, deadline=deadline), device_want(check, cfg))
    if not dev:
        return _skipped(check, out_dir, emit, why, t0, kind="device")

    cap = (smoke_fn or (lambda p, s: android.smoke(p, s, serial=dev, adb_path=adb_path,
                                                   deadline=deadline)))(project, spec_for(check))
    verdict = "pass" if not cap.get("error") else "fail"
    issues = []
    if cap.get("error"):
        issues.append(str(cap["error"]))
        issues += [str(c) for c in (cap.get("crash") or [])[:CRASH_ISSUES]]
    lines = ["device check: %s (mode: %s)" % (check.name, mode),
             "device: %s%s" % (cap.get("device") or dev,
                               "  app: %s" % cap["pkg"] if cap.get("pkg") else ""),
             "apk: %s" % (cap.get("apk") or "-"), "", "on the device:", cap.get("log") or "(nothing)"]
    lines += ["", "verdict: %s" % verdict] + (["issues:"] + ["- " + i for i in issues] if issues else [])
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok = verdict == "pass"
    res = {"name": check.name, "cmd": "android: install + start %s" % (cap.get("apk") or "the debug APK"),
           "type": "android-smoke", "mode": mode, "ok": ok, "exit_code": 0 if ok else 1,
           "verdict": verdict, "issues": issues, "screenshots": [], "passed": None, "failed": None,
           "skipped": None, "seconds": round(time.time() - t0, 1), "output_file": str(log),
           "timed_out": False}
    _cap_state(cap, res)
    if not ok:
        res["first_error"] = "\n".join(issues + [str(cap.get("log") or "")])[:3000]
    if emit:
        emit("checks", "Device check \"%s\": %s" % (check.name, WORD.get(verdict, verdict)),
             {"colour": COLOUR.get(verdict, "yellow")})
    return res


def runners(emit: Callable = None, cfg: dict = None, web: dict = None, model_url: str = "",
            adb_path=None, call: Callable = None, capture_fn: Callable = None,
            smoke_fn: Callable = None, budget: int = None) -> dict:
    """The extra_runners additions for Android: "android-smoke", plus a "vision" that sends
    a target "android" check here and every other screen check to the 6a runner in `web`."""
    cfg = cfg or {}

    def vision_runner(project, check, out_dir):
        if (check.extra or {}).get("target") == "android":
            return run_check(project, check, out_dir, emit=emit, cfg=cfg, adb_path=adb_path,
                             model_url=model_url, call=call, capture_fn=capture_fn, budget=budget)
        if (web or {}).get("vision"):
            return web["vision"](project, check, out_dir)
        res = vision.to_check_result(check.name, {"verdict": "inconclusive", "issues": [NO_RUNNER]},
                                     "advisory")
        res.update({"cmd": "vision", "url": ""})
        return res

    def smoke_runner(project, check, out_dir):
        return run_smoke(project, check, out_dir, emit=emit, cfg=cfg, adb_path=adb_path,
                         smoke_fn=smoke_fn, budget=budget)

    return {"vision": vision_runner, "android-smoke": smoke_runner}
