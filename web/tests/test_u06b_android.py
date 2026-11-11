#!/usr/bin/env python3
r"""Upgrade 6b: Android screen checks - install the debug APK, start it, look at it.

The device is a fake adb script (tests/fixtures/fakeadb/adb) put on PATH for these tests: it
answers `devices`, `install`, `pm list packages`, `resolve-activity`, `am start`, `pidof`,
`logcat`, `exec-out screencap -p` and `input ...` from one JSON scenario, returns real PNGs
whose pixels change per frame, and writes every argument list it was called with into a log
file - which is how these tests assert the exact device commands. No real device, no
emulator, no model server (that part is an in-process fake, as in 6a).

* Engine    - the command sequence for install/launch/screenshot, the stable-screen wait
              (two matching frames 1 s apart, at most max_wait), crash detection with pidof
              and with logcat when the device has no pidof, install/start failures, the APK
              search (inside the project folder only), which package the APK installed, the
              device steps and their validation, and the device choice.
* Checks    - android-smoke (pass / crash / no device), the Android screen check (model
              verdict, a crash is a fail rather than "not sure"), the no-device case never
              failing a job, the time budget, and the runner dispatch.
* Records   - what a valid entry looks like, the frozen job record, and the default checks
              an Android project gets.
* Pages     - the Checks card row, the badge, the review bundle, the fix prompt.
"""
import http.server, json, os, sys, tempfile, threading, unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB))

import androidcheck, badges, upgrades, visioncheck   # noqa: E402
from agentjobs_upgrades import android as ad, checks as ck, verify as vf   # noqa: E402

FAKE_ADB = WEB / "tests" / "fixtures" / "fakeadb" / "adb"


# ------------------------------------------------------------------ the fake device

class DeviceCase(unittest.TestCase):
    """Puts the fake adb on PATH and gives each test its scenario."""

    def setUp(self):
        self.saved = dict(os.environ)
        self.tmp = Path(tempfile.mkdtemp(prefix="u06b-"))
        os.environ["PATH"] = str(FAKE_ADB.parent) + os.pathsep + os.environ.get("PATH", "")
        os.environ["ANDROID_ADB"] = ""            # ignore anything set outside the test
        self.logfile = self.tmp / "adb-calls.txt"
        os.environ["FAKE_ADB"] = "{}"
        os.environ["FAKE_ADB_LOG"] = str(self.logfile)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def scenario(self, **sc):
        os.environ["FAKE_ADB"] = json.dumps(sc)
        return sc

    def calls(self):
        return self.logfile.read_text().splitlines() if self.logfile.exists() else []

    def device_saw(self):
        """Everything the fake adb was asked to do, as one string (for substring checks)."""
        return "\n".join(self.calls())

    def project(self, apk=True, gradle=True):
        p = self.tmp / "proj"
        if apk:
            f = p / "app/build/outputs/apk/debug/app-debug.apk"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"PK not really an apk")
        if gradle:      # a second run finds the app already installed, so this is the fallback
            (p / "app").mkdir(parents=True, exist_ok=True)
            (p / "app/build.gradle").write_text('android {\n  applicationId "com.example.app"\n}\n')
        return p

    def adb(self, serial="", **kw):
        return ad.Adb(serial, str(FAKE_ADB), **kw)


# ------------------------------------------------------------------ the engine

class Engine(DeviceCase):
    def test_adb_is_the_one_on_the_path(self):
        self.assertEqual(ad.find_adb(), str(FAKE_ADB))
        self.assertEqual(ad.find_adb("/nope/adb"), str(FAKE_ADB))      # a bad setting falls back
        os.environ["ANDROID_ADB"] = str(FAKE_ADB)
        self.assertEqual(ad.find_adb(), str(FAKE_ADB))                 # the setting wins over PATH

    def test_devices_and_the_device_choice(self):
        self.scenario(devices=["emulator-5554", "1a2b3c"])
        self.assertEqual(ad.devices(self.adb()), ["emulator-5554", "1a2b3c"])
        self.assertEqual(ad.pick_device(self.adb()), ("emulator-5554", ""))
        self.assertEqual(ad.pick_device(self.adb(), "1a2b3c"), ("1a2b3c", ""))
        # a {{ANDROID_DEVICE}} nobody filled in is the same as asking for any device
        self.assertEqual(ad.pick_device(self.adb(), "{{ANDROID_DEVICE}}"), ("emulator-5554", ""))
        dev, why = ad.pick_device(self.adb(), "not-here")
        self.assertEqual(dev, "")
        self.assertIn("not-here is not connected", why)
        self.assertIn("emulator-5554", why)
        self.assertEqual(self.calls()[0], "devices")

    def test_no_device_and_no_adb_say_so(self):
        self.scenario(devices=[])
        dev, why = ad.pick_device(self.adb())
        self.assertEqual((dev, why), ("", ad.NO_DEVICE))
        dev, why = ad.pick_device(ad.Adb("", "", deadline=None))      # no adb at all
        self.assertEqual((dev, why), ("", ad.NO_ADB))

    def test_install_launch_screenshot_and_the_exact_commands(self):
        self.scenario(devices=["emulator-5554"])
        p = self.project()
        cap = ad.capture(p, {}, adb_path=str(FAKE_ADB), serial="emulator-5554")
        self.assertIsNone(cap["error"])
        self.assertEqual(cap["pkg"], "com.example.app")
        self.assertEqual(cap["device"], "emulator-5554")
        self.assertTrue(cap["stable"])
        self.assertEqual([n for n, _ in cap["shots"]], ["final"])
        self.assertTrue(cap["shots"][0][1].startswith(b"\x89PNG"))
        seen = self.calls()
        self.assertIn("shell pm list packages -3", self.device_saw())
        install = [c for c in seen if " install " in c]
        self.assertEqual(len(install), 1)               # -r keeps the app's data between runs
        self.assertIn("install -r ", install[0])
        self.assertTrue(install[0].endswith("/app/build/outputs/apk/debug/app-debug.apk"))
        for want in ("shell cmd package resolve-activity --brief com.example.app",
                     "shell am force-stop com.example.app", "shell logcat -c",
                     "shell am start -W -n com.example.app/com.example.app.MainActivity",
                     "exec-out screencap -p", "shell pidof com.example.app",
                     "shell logcat -d -t 200"):
            self.assertIn(want, self.device_saw())
        self.assertEqual([c for c in seen if any(ch in c for ch in "|;&`$")], [])   # never a shell
        self.assertIn("$ install", cap["log"])
        self.assertIn("the screen held still after 2 screenshots", cap["log"])

    def test_the_stable_wait_needs_two_matching_frames(self):
        self.scenario(frames=["a", "b", "c", "c", "c"])
        waited = []
        adb = self.adb("emulator-5554", sleep=waited.append)
        png, stable, shots = ad.wait_stable(adb, gap=1.0, max_wait=20.0)
        self.assertTrue(stable)
        self.assertEqual(shots, 4)                                   # a b c c -> the 3rd and 4th match
        self.assertEqual(len(waited), 3)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_a_screen_that_keeps_moving_is_said_so_and_still_checked(self):
        self.scenario(frames=["a", "b", "c", "d", "e"])
        cap = ad.capture(self.project(), {"stable_gap_s": 0.2, "stable_max_s": 0.4},
                         adb_path=str(FAKE_ADB), serial="emulator-5554")
        self.assertFalse(cap["stable"])
        self.assertIsNone(cap["error"])                 # a busy screen is not the app's fault
        self.assertIn("the screen was still changing after 3 screenshots", cap["log"])
        self.assertTrue(cap["shots"][0][1].startswith(b"\x89PNG"))   # the last frame is used

    def test_the_stable_wait_never_waits_longer_than_its_budget(self):
        self.scenario(frames=["a", "b", "c", "d", "e", "f", "g"])
        clock = [0.0]
        def sleep(s):
            clock[0] += s
        adb = self.adb("emulator-5554", sleep=sleep, now=lambda: clock[0])
        png, stable, shots = ad.wait_stable(adb, gap=1.0, max_wait=3.0)
        self.assertFalse(stable)
        self.assertLessEqual(clock[0], 3.0)
        self.assertGreaterEqual(shots, 3)                            # it kept trying
        self.assertTrue(png.startswith(b"\x89PNG"))                  # and still has a picture

    def test_a_crash_is_reported_with_its_logcat_lines(self):
        self.scenario(crash=True)
        cap = ad.capture(self.project(), {}, adb_path=str(FAKE_ADB), serial="emulator-5554")
        self.assertTrue(cap["fatal"])
        self.assertTrue(cap["error"].startswith("App crashed on start"))
        self.assertIn("FATAL EXCEPTION: main", cap["crash"][0])
        self.assertTrue(any("com.example.app" in line for line in cap["crash"]))

    def test_crash_detection_without_pidof_uses_logcat(self):
        self.scenario(no_pidof=True, crash=True)
        adb = self.adb("emulator-5554")
        self.assertIsNone(ad.pid(adb, "com.example.app"))            # the device has no pidof
        self.assertEqual(ad.app_crashed(adb, "com.example.app")[0], True)
        self.scenario(no_pidof=True)
        adb = self.adb("emulator-5554")
        self.assertEqual(ad.app_crashed(adb, "com.example.app"), (False, []))

    def test_a_crash_of_another_app_is_not_ours(self):
        self.scenario(logcat=["E AndroidRuntime: FATAL EXCEPTION: main",
                              "E AndroidRuntime: Process: com.someone.else, PID: 9"])
        self.assertEqual(ad.crash_lines(self.adb("d"), "com.example.app"), [])

    def test_install_failure_and_a_refused_start(self):
        self.scenario(install_fail=True)
        cap = ad.capture(self.project(), {}, adb_path=str(FAKE_ADB), serial="d")
        self.assertTrue(cap["error"].startswith("The app would not install"))
        self.assertIn("INSTALL_FAILED_UPDATE_INCOMPATIBLE", cap["error"])
        self.scenario(start_fail=True)
        cap = ad.capture(self.project(), {}, adb_path=str(FAKE_ADB), serial="d")
        self.assertTrue(cap["error"].startswith("The app did not start"))
        self.assertIn("does not exist", cap["error"])

    def test_no_apk_and_an_apk_outside_the_project(self):
        cap = ad.capture(self.project(apk=False), {}, adb_path=str(FAKE_ADB), serial="d")
        self.assertEqual((cap["error"], cap["fatal"]), (ad.NO_APK, True))
        p = self.project()
        self.assertIsNone(ad.find_apk(p, "../secrets.apk")[0])
        self.assertIsNone(ad.find_apk(p, "/etc/hosts.apk")[0])
        self.assertIsNone(ad.find_apk(p, "app/build/outputs/apk/debug/nope.apk")[0])
        self.assertEqual(ad.find_apk(p, "app/build/outputs/apk/debug/app-debug.apk")[0].name,
                         "app-debug.apk")

    def test_the_app_has_no_launcher_activity(self):
        self.scenario(activity="")
        cap = ad.capture(self.project(), {}, adb_path=str(FAKE_ADB), serial="d")
        self.assertIn("no launcher activity", cap["error"])
        self.assertTrue(cap["fatal"])

    def test_which_app_the_apk_installed(self):
        p = self.project(gradle=True)
        self.scenario(installed=["com.example.app"])
        self.assertEqual(ad.capture(p, {}, adb_path=str(FAKE_ADB), serial="d")["pkg"], "com.example.app")
        self.scenario(installed=["com.example.app"], pkg="com.other.name")
        cap = ad.capture(p, {"pkg": "com.other.name"}, adb_path=str(FAKE_ADB), serial="d")
        self.assertEqual(cap["pkg"], "com.other.name")
        self.assertIn("resolve-activity --brief com.other.name", self.device_saw())
        # the device will not say (nothing new installed): the applicationId in the build file
        self.scenario(installed=[])
        self.assertEqual(ad.capture(p, {}, adb_path=str(FAKE_ADB), serial="d")["pkg"], "com.example.app")

    def test_the_steps_are_sent_to_the_device_with_a_shot_after_each(self):
        self.scenario(frames=["a", "a", "b", "b", "c", "c", "d", "d", "d"])
        cap = ad.capture(self.project(), {"steps": [{"tap": [360, 1900]}, {"text": "buy milk"},
                                                    {"key": "KEYCODE_ENTER"}, {"wait": 50},
                                                    {"swipe": "10,20,30,40"}, {"shell": "rm -rf /"}]},
                         adb_path=str(FAKE_ADB), serial="d")
        calls = self.calls()
        for want in ("shell input tap 360 1900", "shell input text buy%smilk",
                     "shell input keyevent KEYCODE_ENTER", "shell input swipe 10 20 30 40"):
            self.assertIn(want, self.device_saw())
        self.assertNotIn("rm -rf", " ".join(calls))
        self.assertEqual([n for n, _ in cap["shots"]], ["start", "step1", "step2", "step3",
                                                        "step4", "step5", "step6", "final"])
        self.assertEqual([s["error"] for s in cap["steps"]],
                         [None, None, None, None, None, "unknown step"])

    def test_a_refused_step_is_reported_but_the_check_goes_on(self):
        self.scenario(steps_fail=True)
        cap = ad.capture(self.project(), {"steps": [{"tap": [1, 2]}]}, adb_path=str(FAKE_ADB), serial="d")
        self.assertIn("the device refused this step", cap["steps"][0]["error"])
        self.assertEqual([n for n, _ in cap["shots"]], ["start", "step1", "final"])
        self.assertIsNone(cap["error"])

    def test_step_words_are_validated(self):
        self.assertEqual(ad.step_words({"tap": [10, 20]}), (["input", "tap", "10", "20"], ""))
        self.assertEqual(ad.step_words({"tap": "10,20"}), (["input", "tap", "10", "20"], ""))
        self.assertEqual(ad.step_words({"wait": 250}), (None, None))
        self.assertEqual(ad.step_words({"text": "a b"})[0], ["input", "text", "a%sb"])
        for bad in ({"text": "two\nlines"}, {"text": "naïve"}, {"text": "x" * 300},
                    {"key": "rm -rf"}, {"key": "KEYCODE;ls"}, {"tap": [1]}, {"tap": [1, "x"]},
                    {"swipe": [1, 2, 3]}, {"shell": "ls"}, "tap the button", {}):
            words, why = ad.step_words(bad)
            self.assertIsNone(words, bad)
            self.assertTrue(why, bad)

    def test_a_text_step_is_only_ever_typed(self):
        """Plain text still works, and so does everything a normal field accepts."""
        for ok in ("buy milk", "hello@example.com", "what is this", "50% off", "a/b-c.d:e+f",
                   "Item 1", "42", "price: 3.50", "^ _ - = ,"):
            words, why = ad.step_words({"text": ok})
            self.assertEqual(words, ["input", "text", ok.replace(" ", "%s")], (ok, why))
            self.assertEqual(why, "")

    def test_text_that_the_phone_would_run_is_refused(self):
        """# U06b: adb joins the words of `adb shell` and the PHONE's shell parses them, so a
        text step holding shell metacharacters is a command on the device. Every one the
        review named is refused, and step_words says why."""
        for bad in ("a; pm uninstall com.example.app", "a | ls", "a & bg", "$(reboot)",
                    "`id`", "a(b)", "a<b", "a>c", "a\\b", 'say "hi"', "it's", "a\nb",
                    "a*b", "a?b", "a~b", "a#b", "a!b", "a{b}", "a[b]"):
            words, why = ad.step_words({"text": bad})
            self.assertIsNone(words, bad)
            self.assertTrue(why, bad)
        self.assertIsNone(ad.TEXT_RE.match("a; pm uninstall x"))
        for ch in ad.SHELL_META:
            self.assertTrue(ad.SHELL_META_RE.search("x" + ch + "y"), repr(ch))
        self.assertIsNone(ad.SHELL_META_RE.search("buy milk, 100% - ok"))

    def test_the_shell_guard_never_sends_a_dangerous_word_to_adb(self):
        """Every value that goes through `adb shell` is checked at one place, whatever
        produced it - a check step, a package name or a device that answers badly."""
        self.scenario(devices=["d"])
        adb = self.adb("d")
        for words in (("input", "text", "a; pm uninstall x"), ("pidof", "com.x; reboot"),
                      ("am", "start", "-W", "-n", "com.x/com.$(reboot)"), ("logcat", "-d`id`")):
            rc, out, err = adb.shell(*words)
            self.assertNotEqual(rc, 0, words)
            self.assertIn("the phone's shell would run", err)
        self.assertEqual(self.calls(), [])               # nothing of that ever reached adb
        self.assertIn("refused", "\n".join(adb.log))
        adb2 = self.adb("d")                             # a normal command still goes through
        self.assertEqual(adb2.shell("pidof", "com.example.app")[0], 0)
        self.assertEqual(self.calls(), ["-s d shell pidof com.example.app"])

    def test_a_package_name_the_device_invents_never_goes_back_into_the_shell(self):
        self.scenario(devices=["d"], packages=["com.x; reboot", "com.example.app"])
        self.assertEqual(ad.package_list(self.adb("d")), {"com.example.app"})

    def test_a_dangerous_text_step_is_never_sent_to_the_device(self):
        self.scenario(devices=["d"], installed=["com.example.app"])
        bad = ("a; pm uninstall com.example.app", "$(reboot)", "id > /sdcard/x")
        cap = ad.capture(self.project(), {"steps": [{"text": bad[0]}, {"text": bad[1]},
                                                     {"text": "buy milk"}, {"text": bad[2]}]},
                         adb_path=str(FAKE_ADB), serial="d")
        seen = self.device_saw()
        for never in ("uninstall", "reboot", "/sdcard"):
            self.assertNotIn(never, seen)
        self.assertIn("shell input text buy%smilk", seen)     # the safe one is still typed
        self.assertEqual([s["error"] for s in cap["steps"]],
                         [ad.step_words({"text": t})[1] for t in (bad[0], bad[1])]
                         + [None, ad.step_words({"text": bad[2]})[1]])


# ------------------------------------------------------------------ android-smoke

class Smoke(DeviceCase):
    CHECK = ck.Check("app runs", "", type="android-smoke", extra={"mode": "required"})

    def run_one(self, check=None, **sc):
        self.scenario(**sc)
        return androidcheck.run_smoke(self.project(), check or self.CHECK, self.tmp / "out",
                                      cfg={}, adb_path=str(FAKE_ADB))

    def test_the_app_starts_and_stays_up(self):
        res = self.run_one(devices=["emulator-5554"], installed=["com.example.app"])
        self.assertEqual((res["verdict"], res["ok"], res["exit_code"], res["type"]),
                         ("pass", True, 0, "android-smoke"))
        self.assertEqual((res["mode"], res["device"], res["pkg"]),
                         ("required", "emulator-5554", "com.example.app"))
        log = Path(res["output_file"]).read_text()
        self.assertIn("device check: app runs (mode: required)", log)
        self.assertIn("$ install -r", log)
        self.assertIn("$ shell am start -W -n", log)
        self.assertIn("verdict: pass", log)

    def test_a_crash_fails_it_and_carries_the_logcat_lines(self):
        res = self.run_one(devices=["emulator-5554"], crash=True)
        self.assertEqual((res["verdict"], res["ok"], res["mode"]), ("fail", False, "required"))
        self.assertTrue(res["issues"][0].startswith("App crashed on start"))
        self.assertTrue(any("FATAL EXCEPTION" in i for i in res["issues"]))
        self.assertIn("FATAL EXCEPTION", res["first_error"])
        self.assertIn("FATAL EXCEPTION", Path(res["output_file"]).read_text())
        self.assertTrue(res["crash"])
        self.assertEqual(androidcheck.short_result(res), "crashed on start")

    def test_no_device_is_skipped_and_never_fails_the_job(self):
        res = self.run_one(devices=[])
        self.assertEqual((res["verdict"], res["mode"], res["ok"]), ("skipped", "advisory", False))
        self.assertEqual(res["issues"], [ad.NO_DEVICE])
        self.assertEqual(ck.summarize([res], "file")["status"], "unverified")
        self.assertEqual(ck.banner_text(ck.summarize([res], "file"))[0], "yellow")
        self.assertIn(ad.NO_DEVICE, Path(res["output_file"]).read_text())

    def test_no_adb_at_all_is_also_just_a_message(self):
        self.scenario()
        res = androidcheck.run_smoke(self.project(), self.CHECK, self.tmp / "out", cfg={}, adb_path="")
        self.assertEqual(res["verdict"], "skipped")
        self.assertIn("no adb", res["issues"][0].lower())
        self.assertEqual(androidcheck.short_result(res), "no adb")

    def test_the_named_device_from_the_settings_is_used(self):
        self.scenario(devices=["emulator-5554", "phone-7"], installed=["com.example.app"])
        res = androidcheck.run_smoke(self.project(),
                                     ck.Check("app runs", "", type="android-smoke",
                                              extra={"mode": "required", "device": "phone-7"}),
                                     self.tmp / "out", cfg={"android_device": "phone-7"},
                                     adb_path=str(FAKE_ADB))
        self.assertEqual((res["verdict"], res["device"]), ("pass", "phone-7"))

    def test_the_hold_time_comes_from_the_check(self):
        self.scenario(devices=["d"], installed=["com.example.app"])
        res = androidcheck.run_smoke(self.project(),
                                     ck.Check("app runs", "", type="android-smoke",
                                              extra={"mode": "required", "hold_s": 1}),
                                     self.tmp / "out", cfg={}, adb_path=str(FAKE_ADB))
        self.assertEqual(res["verdict"], "pass")
        self.assertIn("shell pidof com.example.app", self.device_saw())


# ------------------------------------------------------------------ the Android screen check

class FakeModel:
    """An OpenAI-shaped /v1/chat/completions that answers from a list (the last answer
    repeats). .requests holds every body it was sent."""

    def __init__(self, answers=()):
        self.answers, self.requests = list(answers), []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    body = {}
                outer.requests.append(body)
                text = outer.answers[min(len(outer.requests) - 1, len(outer.answers) - 1)]
                payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/v1/chat/completions" % self.srv.server_address[1]

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def vcheck(name="looks right", **extra):
    base = {"target": "android", "spec": "a list of notes and a + button bottom right",
            "mode": "advisory"}
    base.update(extra)
    return ck.Check(name, "", type="vision", extra=base)


class ScreenCheck(DeviceCase):
    def run_one(self, check=None, answers=('{"pass": true, "issues": [], "confidence": 0.9}',) * 2,
            apk=True, **sc):
        self.scenario(**sc)
        fake = FakeModel(answers)
        try:
            res = androidcheck.run_check(self.project(apk=apk), check or vcheck(), self.tmp / "out",
                                         cfg={}, adb_path=str(FAKE_ADB), model_url=fake.url)
        finally:
            fake.stop()
        return res, fake

    def test_the_app_is_screened_and_the_model_is_asked_twice(self):
        res, fake = self.run_one(devices=["emulator-5554"], installed=["com.example.app"])
        self.assertEqual((res["verdict"], res["ok"], res["type"]), ("pass", True, "vision"))
        self.assertEqual(len(fake.requests), 2)
        parts = fake.requests[0]["messages"][0]["content"]
        sent = next(p["image_url"]["url"] for p in parts if p.get("type") == "image_url")
        self.assertTrue(sent.startswith("data:image/png;base64,"))
        self.assertIn("a list of notes", next(p["text"] for p in parts if p.get("type") == "text"))
        self.assertEqual((res["device"], res["pkg"]), ("emulator-5554", "com.example.app"))
        shots = [Path(p) for p in res["screenshots"]]
        self.assertEqual([p.name for p in shots], ["vision-looks_right-1-final.png"])
        self.assertEqual(shots[0].read_bytes()[:4], b"\x89PNG")
        self.assertIn("$ shell am start", Path(res["output_file"]).read_text())

    def test_a_crash_is_a_real_fail_and_the_model_is_not_asked(self):
        res, fake = self.run_one(devices=["emulator-5554"], crash=True)
        self.assertEqual((res["verdict"], res["ok"], res["asked"] if "asked" in res else 0),
                         ("fail", False, 0))
        self.assertEqual(fake.requests, [])
        self.assertTrue(res["issues"][0].startswith("App crashed on start"))
        self.assertTrue(any("FATAL EXCEPTION" in i for i in res["issues"]))
        self.assertEqual(androidcheck.short_result(res), "crashed on start")

    def test_a_missing_apk_is_a_fail_not_a_shrug(self):
        res, _ = self.run_one(devices=["emulator-5554"], apk=False)
        self.assertEqual((res["verdict"], res["mode"]), ("fail", "advisory"))
        self.assertIn("APK not built", res["issues"][0])
        self.assertEqual(androidcheck.short_result(res), "no APK built")

    def test_no_device_is_skipped_and_advisory_even_when_required(self):
        self.scenario(devices=[])
        res = androidcheck.run_check(self.project(), vcheck(mode="required"), self.tmp / "out",
                                     cfg={}, adb_path=str(FAKE_ADB))
        self.assertEqual((res["verdict"], res["mode"], res["ok"]), ("skipped", "advisory", False))
        self.assertEqual(res["issues"], [ad.NO_DEVICE])
        self.assertEqual(visioncheck.WORD[res["verdict"]], "no device")

    def test_the_steps_and_the_time_budget_reach_the_device(self):
        self.scenario(devices=["d"], installed=["com.example.app"], frames=["a", "a", "b", "b"])
        res = androidcheck.run_check(self.project(), vcheck(steps=[{"tap": [5, 6]}]),
                                     self.tmp / "out", cfg={}, adb_path=str(FAKE_ADB),
                                     call=lambda body: '{"pass": true}', budget=60)
        self.assertIn("shell input tap 5 6", self.device_saw())
        self.assertEqual(res["verdict"], "pass")
        # an Android check that runs out of time says so instead of hanging the gate
        res = androidcheck.run_check(self.project(), vcheck(), self.tmp / "out2", cfg={},
                                     adb_path=str(FAKE_ADB), call=lambda body: '{"pass": true}',
                                     budget=0)
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertIn("ran out of time", Path(res["output_file"]).read_text())

    def test_the_runner_sends_web_checks_to_6a_and_android_ones_here(self):
        seen = []
        web = {"vision": lambda p, c, o: seen.append(c.name) or {"name": c.name, "type": "vision"}}
        self.scenario(devices=["d"], installed=["com.example.app"])
        runners = androidcheck.runners(cfg={}, adb_path=str(FAKE_ADB), web=web,
                                       call=lambda body: '{"pass": true}')
        out = self.tmp / "out"
        r_web = runners["vision"](self.project(), ck.Check("page", "", type="vision",
                                                           extra={"target": "web"}), out)
        r_and = runners["vision"](self.project(), vcheck(), out)
        r_smoke = runners["android-smoke"](self.project(),
                                           ck.Check("app runs", "", type="android-smoke"), out)
        self.assertEqual(seen, ["page"])
        self.assertEqual(r_web["type"], "vision")
        self.assertEqual(r_and["verdict"], "pass")
        self.assertEqual(r_smoke["verdict"], "pass")
        # with no 6a runner registered the web check is reported, not dropped
        lonely = androidcheck.runners(cfg={}, adb_path=str(FAKE_ADB))
        self.assertEqual(lonely["vision"](self.tmp, vcheck(name="page", target="web"), out)["issues"],
                         [androidcheck.NO_RUNNER])


# ------------------------------------------------------------------ the check record

class Record(unittest.TestCase):
    SMOKE = {"name": "app runs", "type": "android-smoke", "mode": "required"}
    SCREEN = {"name": "looks right", "type": "vision", "target": "android",
              "spec": "a list of notes and a + button", "steps": [{"tap": [10, 20]},
                                                                  {"text": "milk"},
                                                                  {"key": "KEYCODE_ENTER"}],
              "device": "emulator-5554", "apk": "app/build/outputs/apk/debug/app-debug.apk"}

    def test_the_documented_shape_is_accepted(self):
        self.assertTrue(androidcheck.check_ok(dict(self.SMOKE)))
        self.assertTrue(androidcheck.check_ok(dict(self.SCREEN)))
        self.assertTrue(androidcheck.check_ok({"name": "n", "type": "vision", "target": "android",
                                               "spec": "x"}))

    def test_junk_is_refused(self):
        for bad in ({**self.SMOKE, "mode": "must"}, {**self.SMOKE, "hold_s": 0},
                    {**self.SMOKE, "hold_s": 9999}, {**self.SMOKE, "colour": "red"},
                    {**self.SCREEN, "spec": ""}, {**self.SCREEN, "target": "web"},
                    {"name": "n", "type": "vision", "spec": "x"},
                    {**self.SCREEN, "apk": "/etc/passwd"}, {**self.SCREEN, "apk": "../x.apk"},
                    {**self.SCREEN, "pkg": "not a package"}, {**self.SCREEN, "pkg": "a;rm -rf"},
                    {**self.SCREEN, "device": "one two"}, {**self.SCREEN, "device": "x" * 200},
                    {**self.SCREEN, "steps": [{"shell": "ls"}]}, {**self.SCREEN, "steps": "tap it"},
                    {**self.SCREEN, "steps": [{"tap": [1, 2, 3]}]},
                    # U06b: a text step is typed through the phone's shell, so a step that
                    # would run something there is not a check at all
                    {**self.SCREEN, "steps": [{"text": "a; pm uninstall x"}]},
                    {**self.SCREEN, "steps": [{"text": "$(reboot)"}]},
                    {**self.SCREEN, "votes": 9}, {"name": "n", "type": "vision",
                                                  "target": "android", "spec": "x" * 5000}):
            self.assertFalse(androidcheck.check_ok(bad), bad)

    def test_a_check_from_the_project_is_frozen_into_the_job(self):
        t = Path(tempfile.mkdtemp(prefix="u06b-"))
        (t / "AGENT_CHECKS.json").write_text(json.dumps({"checks": [
            {"name": "tests", "cmd": "make test"},
            dict(self.SMOKE), dict(self.SCREEN, name="the screen")]}))
        m = {}
        upgrades.freeze_checks(m, t)
        self.assertEqual([c.get("type", "command") for c in m["checks"]],
                         ["command", "android-smoke", "vision"])
        self.assertTrue(upgrades.checks_ok(m["checks"]))
        got = {c.name: c for c in upgrades.job_checks(m)}
        self.assertEqual(got["the screen"].extra["target"], "android")
        self.assertEqual(got["the screen"].extra["steps"], self.SCREEN["steps"])
        self.assertEqual(got["app runs"].type, "android-smoke")
        self.assertEqual(got["app runs"].extra["mode"], "required")

    def test_a_broken_android_entry_is_dropped(self):
        t = Path(tempfile.mkdtemp(prefix="u06b-"))
        (t / "AGENT_CHECKS.json").write_text(json.dumps({"checks": [
            {"name": "app runs", "type": "android-smoke", "pkg": "rm -rf /"}]}))
        m = {}
        upgrades.freeze_checks(m, t)
        self.assertNotIn("checks", m)

    def test_web_vision_checks_are_still_validated_by_6a(self):
        self.assertFalse(upgrades.checks_ok([{"name": "x", "type": "vision", "open": "a.html"}]))
        self.assertTrue(upgrades.checks_ok([{"name": "x", "type": "vision", "open": "a.html",
                                             "spec": "a button"}]))
        self.assertFalse(upgrades.checks_ok([{"name": "x", "type": "android-smoke", "hold_s": 0}]))

    def test_an_android_project_gets_a_smoke_check_by_default(self):
        t = Path(tempfile.mkdtemp(prefix="u06b-"))
        (t / "gradlew").write_text("#!/bin/sh")
        self.assertEqual([c.cmd for c in ck.detect_checks(t)],
                         ["./gradlew --offline assembleDebug", "./gradlew --offline testDebugUnitTest"])
        (t / "app/src/main/AndroidManifest.xml").parent.mkdir(parents=True)
        (t / "app/src/main/AndroidManifest.xml").write_text("<manifest/>")
        checks = ck.detect_checks(t)
        self.assertEqual([c.type for c in checks], ["command", "command", "android-smoke"])
        self.assertEqual(checks[2].name, "app runs")
        self.assertEqual(checks[2].extra["mode"], "required")
        self.assertEqual(ck.load_checks(t)[1], "detected")            # no AGENT_CHECKS.json here
        # the gate reads those detected checks, so they have to be acceptable ones
        frozen = [{"name": c.name, "cmd": c.cmd, "type": c.type, "timeout_s": c.timeout_s,
                   **c.extra} for c in checks]
        self.assertTrue(upgrades.checks_ok(frozen))

    def test_the_device_setting_is_read_from_the_settings_file(self):
        t = Path(tempfile.mkdtemp(prefix="u06b-")) / "settings.json"
        t.write_text(json.dumps({"android_device": "phone-7", "attempts": 4}))
        cfg = upgrades.load_settings(t)
        self.assertEqual(cfg["android_device"], "phone-7")
        self.assertEqual(cfg["attempts"], 4)
        self.assertEqual(upgrades.load_settings(Path(tempfile.mkdtemp()) / "none.json")["android_device"], "")


# ------------------------------------------------------------------ the gate and the pages

class GateAndPages(DeviceCase):
    def test_a_crashing_app_fails_the_job_and_reaches_the_agent(self):
        self.scenario(devices=["d"], crash=True)
        checks = [ck.Check("app runs", "", type="android-smoke", extra={"mode": "required"})]
        runners = androidcheck.runners(cfg={}, adb_path=str(FAKE_ADB))
        summ = ck.run_all(self.project(), self.tmp / "out", checks=checks, extra_runners=runners)
        self.assertEqual(summ["status"], "failed")
        self.assertIn("app runs", ck.banner_text(summ)[1])
        prompt = vf.build_fix_prompt("Add a notes screen", summ, 1, 3)
        self.assertIn("Problems seen on screen:", prompt)
        self.assertIn("App crashed on start", prompt)
        self.assertIn("FATAL EXCEPTION", prompt)

    def test_the_board_runs_both_kinds_of_android_check(self):
        self.scenario(devices=["d"], installed=["com.example.app"], frames=["a", "a"])
        checks = [ck.Check("app runs", "", type="android-smoke", extra={"mode": "required"}),
                  vcheck(mode="required")]
        runners = androidcheck.runners(cfg={}, adb_path=str(FAKE_ADB),
                                       call=lambda body: '{"pass": true}')
        summ = ck.run_all(self.project(), self.tmp / "out", checks=checks, extra_runners=runners)
        self.assertEqual(summ["status"], "passed")
        self.assertEqual([r["type"] for r in summ["results"]], ["android-smoke", "vision"])
        self.assertEqual(ck.banner_text(summ), ("green", "Checks passed"))

    def test_the_card_shows_the_device_the_app_and_the_crash(self):
        m = {"checks": [{"name": "app runs", "type": "android-smoke", "mode": "required"},
                        {"name": "the screen", "type": "vision", "target": "android",
                         "spec": "a score"}],
             "verify": {"rounds": [{"round": 0, "summary": {"status": "failed", "results": [
                 {"name": "app runs", "type": "android-smoke", "cmd": "android: install + start app-debug.apk",
                  "mode": "required", "ok": False, "verdict": "fail", "device": "emulator-5554",
                  "pkg": "com.example.app", "seconds": 12.5,
                  "issues": ["App crashed on start - it was not still running after 10 seconds.",
                             "E AndroidRuntime: FATAL EXCEPTION: main"],
                  "crash": ["E AndroidRuntime: FATAL EXCEPTION: main"]},
                 {"name": "the screen", "type": "vision", "cmd": "vision", "mode": "advisory",
                  "ok": True, "verdict": "pass", "device": "emulator-5554", "issues": [],
                  "screenshots": [], "seconds": 9.0}]}}]}}
        h = upgrades.checks_card_html(m, "j1", True)
        self.assertIn("install and start the app on a phone", h)     # the frozen-check line
        self.assertIn("screenshot of the app on a phone", h)
        self.assertIn("emulator-5554", h)
        self.assertIn("com.example.app", h)
        self.assertIn("crashed on start", h)
        self.assertIn("FATAL EXCEPTION", h)
        self.assertIn("the app ran", upgrades._android_row(0, {"name": "app runs", "ok": True,
                                                               "verdict": "pass", "seconds": 3}))

    def test_the_bundle_names_the_device_and_the_crash_lines(self):
        m = {"verify": {"status": "failed", "banner": {"colour": "red", "text": "Checks failed: app runs"},
                        "rounds": [{"round": 0, "summary": {"status": "failed", "results": [
                            {"name": "app runs", "type": "android-smoke", "mode": "required",
                             "ok": False, "verdict": "fail", "device": "emulator-5554",
                             "pkg": "com.example.app", "apk": "/p/app-debug.apk",
                             "issues": ["App crashed on start"],
                             "crash": ["E AndroidRuntime: FATAL EXCEPTION: main"]}]}}]}}
        md = upgrades.bundle_section(m)
        self.assertIn("device check `app runs`: fail (required)", md)
        self.assertIn("device emulator-5554, app com.example.app, apk app-debug.apk", md)
        self.assertIn("logcat: E AndroidRuntime: FATAL EXCEPTION: main", md)

    def test_badges_for_a_device_check(self):
        m = {"verify": {"rounds": [{"round": 0, "summary": {"status": "unverified", "results": [
            {"name": "app runs", "type": "android-smoke", "ok": False, "verdict": "skipped"},
            {"name": "the screen", "type": "vision", "ok": False, "verdict": "inconclusive"}]}}]}}
        self.assertEqual([b["text"] for b in badges.badges(m, "j1")],
                         ["app runs (no device)", "the screen (not sure)"])


if __name__ == "__main__":
    unittest.main()
