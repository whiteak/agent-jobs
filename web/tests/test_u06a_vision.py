#!/usr/bin/env python3
r"""Upgrade 6a: vision checks - screenshot the app, ask the model whether it looks right.

* Sizing    - PNG width read, the max-1280 px downscale (exact pixels), an undecodable PNG
              passed through with a note instead of a crash.
* Verdicts  - the model server is a fake in-process http server with canned answers:
              both pass / both fail / garbage (after the retry) / a disagreeing pair / a
              server that refuses - and what the check result and its log say.
* Capture   - the jail command is built and (for the Python driver) actually run: this
              sandbox has no Playwright, so it proves the driver answers cleanly and the
              engine calls that "not sure" instead of failing the job. The envelope is
              also fed by hand (two shots, garbage, no answer).
* Records   - AGENT_CHECKS.json -> frozen job record -> checks.Check keeps type + extras.
* Pages     - the Checks card (verdict, issues, screenshot thumbnails), the review-bundle
              lines, the badge for an unsure check, and /api/shot through the real
              handler (inside JOBS/<id>/ only, login guard).

Nothing here calls the real model server (127.0.0.1:8080) or a real browser.
"""
import base64, http.server, json, shutil, socket, struct, subprocess, sys, tempfile, threading, unittest, urllib.error, urllib.request, zlib
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WEB))

import badges, jobboard, upgrades, visioncheck   # noqa: E402
from agentjobs_upgrades import checks as ck, verify as vf   # noqa: E402


def png(w, h, ch=4, pixel=None):
    """A plain 8-bit PNG with filter 0 - the shape Playwright writes. pixel(x, y) -> bytes."""
    rows = b"".join(b"\x00" + b"".join(pixel(x, y) for x in range(w)) for y in range(h))
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[ch]

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ctype, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def small_png():
    return png(40, 20, pixel=lambda x, y: bytes([x * 3 % 256, y * 5 % 256, 200, 255]))


def vcheck(name="looks right", **extra):
    """A vision check as checks.load_checks would hand it over."""
    base = {"target": "web", "open": "index.html", "spec": "a red orb, a score top right, a Start button",
            "steps": [{"click": "#start"}, {"wait": 300}], "size": [1280, 800], "mode": "advisory"}
    base.update(extra)
    return ck.Check(name, "", type="vision", extra=base)


def capture_shots(*shots, error=None, url="http://127.0.0.1:9/index.html"):
    """A stand-in for the browser: [(name, png bytes), ...]."""
    def cap(project, spec):
        return {"shots": list(shots), "url": url, "steps": [{"step": 0, "what": "{}", "error": None}],
                "log": "", "error": error}
    return cap


class FakeModel:
    """An OpenAI-shaped /v1/chat/completions that answers from a list (the last answer
    repeats). .requests holds every body it was sent; status=500 makes it refuse."""

    def __init__(self, answers=(), status=200):
        self.answers, self.status, self.requests = list(answers), status, []
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
                if outer.status != 200:
                    self.send_error(outer.status, "the model refuses")
                    return
                text = outer.answers[min(len(outer.requests) - 1, len(outer.answers) - 1)]
                payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.thr = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thr.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/v1/chat/completions" % self.srv.server_address[1]

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def run_one(check, shots=None, answers=("…",), out=None, error=None, status=200, call=None):
    """One vision check end to end, with a mocked browser and a canned model."""
    t = Path(tempfile.mkdtemp(prefix="u06a-"))
    fake = FakeModel(answers, status=status) if call is None else None
    try:
        res = visioncheck.run_check(t / "proj", check, out or (t / "out"),
                                    capture_fn=capture_shots(*(shots if shots is not None else [("final", small_png())]),
                                                             error=error),
                                    model_url=fake.url if fake else "", call=call)
        return res, fake, t
    finally:
        if fake:
            fake.stop()


# ------------------------------------------------------------------ PNG sizing

class Sizing(unittest.TestCase):
    def test_png_size(self):
        self.assertEqual(visioncheck.png_size(small_png()), (40, 20))
        self.assertIsNone(visioncheck.png_size(b"GIF89a..."))
        self.assertIsNone(visioncheck.png_size(b""))

    def test_small_unchanged(self):
        out, note = visioncheck.fit_width(small_png())
        self.assertEqual((out, note), (small_png(), ""))

    def test_downscale_is_exact(self):
        big = png(4, 2, pixel=lambda x, y: bytes([x * 10, y, x + y, 255]))
        out, note = visioncheck.fit_width(big, 2)
        self.assertEqual(visioncheck.png_size(out), (2, 1))
        dec = visioncheck._png_decode(out)
        self.assertEqual(dec[2], 4)
        self.assertEqual(bytes(dec[3][0]), bytes([0, 0, 0, 255]) + bytes([20, 0, 2, 255]))
        self.assertIn("resized from 4x2 to 2x1", note)

    def test_oversized_screenshot_fits(self):
        out, note = visioncheck.fit_width(png(3000, 2000, pixel=lambda x, y: b"\x10\x20\x30\x40"))
        self.assertEqual(visioncheck.png_size(out), (1280, 853))
        self.assertIn("resized from 3000x2000", note)

    def test_undecodable_png_is_passed_through(self):
        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        broken = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 3000, 2000, 8, 6, 0, 0, 0))
                  + chunk(b"IDAT", b"this is not deflate data") + chunk(b"IEND", b""))
        out, note = visioncheck.fit_width(broken)
        self.assertEqual(out, broken)
        self.assertIn("could not be resized", note)

    def test_gray_png_round_trip(self):
        out, _ = visioncheck.fit_width(png(6, 2, ch=1, pixel=lambda x, y: bytes([x * 40])), 3)
        self.assertEqual(visioncheck.png_size(out), (3, 1))
        self.assertEqual(visioncheck._png_decode(out)[2], 1)


# ------------------------------------------------------------------ the model's verdict

class Verdicts(unittest.TestCase):
    def test_both_votes_pass(self):
        res, fake, _ = run_one(vcheck(), answers=['{"pass": true, "issues": [], "confidence": 0.9}',
                                                  '{"pass": true, "issues": [], "confidence": 0.8}'])
        self.assertEqual((res["verdict"], res["ok"], res["exit_code"], res["type"]), ("pass", True, 0, "vision"))
        self.assertEqual(res["asked"], 2)
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual([Path(p).name for p in res["screenshots"]], ["vision-looks_right-1-final.png"])
        self.assertTrue(Path(res["output_file"]).read_text().startswith("vision check: looks right"))
        self.assertIn('"pass": true', Path(res["output_file"]).read_text())

    def test_two_real_screenshots_are_saved(self):
        res, _, t = run_one(vcheck(), shots=[("before", png(20, 10, pixel=lambda x, y: b"\x01\x02\x03\x04")),
                                             ("after", png(30, 10, pixel=lambda x, y: b"\x05\x06\x07\x08"))],
                            answers=['{"pass": true}', '{"pass": true}'])
        names = [Path(p).name for p in res["screenshots"]]
        self.assertEqual(names, ["vision-looks_right-1-before.png", "vision-looks_right-2-after.png"])
        for p in res["screenshots"]:
            self.assertTrue(Path(p).is_file())
        self.assertIn("page: http://127.0.0.1:9/index.html", Path(res["output_file"]).read_text())

    def test_both_votes_fail(self):
        res, _, _ = run_one(vcheck(), answers=['{"pass": false, "issues": ["no score", "no orb"]}',
                                               '{"pass": false, "issues": ["no orb"]}'])
        self.assertEqual((res["verdict"], res["ok"]), ("fail", False))
        self.assertEqual(res["issues"], ["no score", "no orb"])      # merged, no duplicates
        self.assertEqual(res["mode"], "advisory")

    def test_garbage_after_the_retry_is_not_sure(self):
        res, fake, _ = run_one(vcheck(), answers=["the screen looks fine to me", "still no json"])
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertFalse(res["ok"])
        self.assertEqual(len(fake.requests), 4)                      # 2 votes x (1 + 1 retry)
        self.assertIn("didn't give a clear answer", res["issues"][0])

    def test_votes_that_disagree_are_not_sure(self):
        res, _, _ = run_one(vcheck(), answers=['{"pass": true}', '{"pass": false, "issues": ["no score"]}'])
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertIn("disagreed", res["issues"][0])

    def test_dead_model_server_does_not_break_the_check(self):
        res, _, _ = run_one(vcheck(), answers=[""], status=500)
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertTrue(any("model server did not answer" in i for i in res["issues"]))
        self.assertIn("model call failed", Path(res["output_file"]).read_text())

    def test_no_spec_means_no_screenshot(self):
        def boom(project, spec):
            raise AssertionError("the browser must not be started for a check with no spec")
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        res = visioncheck.run_check(t, vcheck(spec="  "), t / "out", capture_fn=boom)
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertIn("no spec text", res["issues"][0])

    def test_votes_comes_from_the_check(self):
        res, fake, _ = run_one(vcheck(votes=1), answers=['{"pass": true}'])
        self.assertEqual((res["asked"], len(fake.requests)), (1, 1))

    def test_no_time_left_is_not_sure(self):
        """A check cannot eat the whole gate: when its time is gone the model is not asked."""
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        asked = []
        res = visioncheck.run_check(t, vcheck(), t / "out", budget=0,
                                    capture_fn=capture_shots(("final", small_png())),
                                    call=lambda body: asked.append(1) or '{"pass": true}')
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertEqual(asked, [])
        self.assertIn("ran out of time", Path(res["output_file"]).read_text())

    def test_the_picture_the_model_gets_is_at_most_1280_wide(self):
        huge = png(2400, 1500, pixel=lambda x, y: b"\x20\x40\x60\x80")
        res, fake, _ = run_one(vcheck(), shots=[("final", huge)], answers=['{"pass": true}', '{"pass": true}'])
        body = fake.requests[0]
        parts = body["messages"][0]["content"]
        text = next(p["text"] for p in parts if p.get("type") == "text")
        url = next(p["image_url"]["url"] for p in parts if p.get("type") == "image_url")
        sent = base64.b64decode(url.split(",", 1)[1])
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertLessEqual(visioncheck.png_size(sent)[0], 1280)
        self.assertEqual(visioncheck.png_size(Path(res["screenshots"][0]).read_bytes())[0], 1280)
        for s in ("a red orb", "Start button", '"pass"'):
            self.assertIn(s, text)


# ------------------------------------------------------------------ capture

class Capture(unittest.TestCase):
    def test_envelope_round_trip(self):
        obj = {"shots": [{"name": "final", "b64": base64.b64encode(b"png!").decode()}], "url": "u", "error": None}
        raw = b"noise\nVISIONSHOT_JSON " + base64.b64encode(json.dumps(obj).encode()) + b"\n"
        self.assertEqual(visioncheck.parse_envelope(raw)["url"], "u")
        self.assertIsNone(visioncheck.parse_envelope(b"VISIONSHOT_JSON not-base64!!"))
        self.assertIsNone(visioncheck.parse_envelope(b"no marker here"))

    def test_the_jail_gets_the_driver_and_the_spec(self):
        seen = {}

        def spy_wrap(path, cmd):
            seen["path"], seen["cmd"] = path, cmd
            return ["true"]

        out = Path(tempfile.mkdtemp(prefix="u06a-"))
        cap = visioncheck.capture(out, visioncheck.spec_for(vcheck()), wrap=spy_wrap, kind="python")
        self.assertIn("VISION_SPEC_B64=", seen["cmd"])
        self.assertIn("python3 - <<'%s'" % visioncheck.DRIVER_EOF, seen["cmd"])
        self.assertIn("playwright.sync_api", seen["cmd"])            # the driver travels with the command
        self.assertIn("from playwright", visioncheck.driver_source("python"))
        self.assertEqual(cap["shots"], [])
        self.assertIn("did not answer", cap["error"])

    def test_the_real_python_driver_reports_a_missing_browser(self):
        """Nothing is installed here, so this is the honest end-to-end of the command."""
        cap = visioncheck.capture(Path(tempfile.mkdtemp(prefix="u06a-")),
                                  visioncheck.spec_for(vcheck()), kind="python")
        self.assertEqual(cap["shots"], [])
        self.assertIn("Playwright", cap["error"])

    def test_envelope_shots_and_errors(self):
        def fake_run(argv, cwd, timeout):
            obj = {"shots": [{"name": "a", "b64": base64.b64encode(b"one").decode()},
                             {"name": "b", "b64": "abc"}],
                   "url": "http://127.0.0.1:1/x", "steps": [], "error": "the page did not open"}
            return 0, b"VISIONSHOT_JSON " + base64.b64encode(json.dumps(obj).encode()), b"warn\n"

        cap = visioncheck.capture("/p", {}, run=fake_run, kind="node")
        self.assertEqual([s[0] for s in cap["shots"]], ["a"])        # the undecodable one is dropped
        self.assertEqual(cap["url"], "http://127.0.0.1:1/x")
        self.assertEqual(cap["error"], "the page did not open")
        self.assertEqual(cap["log"], "warn")

    def test_a_shot_that_is_not_a_png_is_left_out(self):
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        res = visioncheck.run_check(t, vcheck(), t / "out",
                                    capture_fn=capture_shots(("junk", b"not a png at all"),
                                                             ("final", small_png())),
                                    call=lambda body: '{"pass": true}')
        self.assertEqual([Path(p).name for p in res["screenshots"]], ["vision-looks_right-2-final.png"])
        self.assertIn("is not a PNG - left out", Path(res["output_file"]).read_text())

    def test_capture_stops_cleanly_when_no_browser_exists(self):
        cap = visioncheck.capture(Path(tempfile.mkdtemp(prefix="u06a-")), {})
        self.assertEqual(cap["shots"], [])
        self.assertIn("no Playwright and no Chromium", cap["error"])
        self.assertIn("not a problem with the code", cap["error"])

    def test_no_browser_check_is_not_sure_and_says_why(self):
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        res = visioncheck.run_check(t, vcheck(), t / "out",
                                    capture_fn=lambda pr, sp: {"shots": [], "error": visioncheck.NO_BROWSER})
        self.assertEqual(res["verdict"], "inconclusive")
        self.assertIn("playwright install chromium", res["issues"][0])

    def test_spec_and_steps_are_bounded(self):
        spec = visioncheck.spec_for(vcheck(size=[5000, 9000], open="x" * 400,
                                           steps=[{"click": "#c"}] * 50 + [{"nope": 1}]))
        self.assertEqual(spec["size"], [1280, 4096])
        self.assertEqual(len(spec["open"]), 300)
        self.assertEqual(len(spec["steps"]), 40)                    # STEP_MAX
        self.assertEqual(spec["steps"][0], {"click": "#c"})
        kept = visioncheck.spec_for(vcheck(steps=[{"click": "#a"}, {"nope": 1}, "text",
                                                  {"type": "#in", "text": "y" * 400}]))["steps"]
        self.assertEqual(kept, [{"click": "#a"}, {"type": "#in", "text": "y" * 200}])

    def test_drivers_are_valid(self):
        compile((WEB / "visionshot.py").read_text(), "visionshot.py", "exec")
        src = (WEB / "visionshot.mjs").read_text()
        self.assertIn("from \"playwright\"", src)
        if shutil.which("node"):
            t = Path(tempfile.mkdtemp(prefix="u06a-")) / "driver.mjs"
            t.write_text(src)
            self.assertEqual(subprocess.run(["node", "--check", str(t)], capture_output=True).returncode, 0)


# ------------------------------------------------------------------ the check record

class Record(unittest.TestCase):
    GOOD = {"name": "looks right", "type": "vision", "target": "web", "open": "index.html",
            "steps": [{"click": "#start"}, {"key": " "}, {"wait": 500}],
            "spec": "a start button and a score", "size": [1280, 800], "mode": "advisory"}

    def test_the_documented_shape_is_accepted(self):
        self.assertTrue(visioncheck.check_ok(dict(self.GOOD)))
        self.assertTrue(visioncheck.check_ok({"name": "n", "type": "vision", "open": "a.html", "spec": "x"}))

    def test_junk_is_refused(self):
        for bad in ({**self.GOOD, "spec": ""}, {**self.GOOD, "open": ""}, {**self.GOOD, "mode": "must"},
                    {**self.GOOD, "size": [10, 10]}, {**self.GOOD, "size": "big"},
                    {**self.GOOD, "steps": [{"click": "#a", "extra": 1}]}, {**self.GOOD, "steps": "click it"},
                    {**self.GOOD, "votes": 9}, {**self.GOOD, "colour": "red"},
                    {**self.GOOD, "target": "android"}, {"name": "n", "type": "vision", "open": "a.html",
                                                          "spec": "x" * 5000}):
            self.assertFalse(visioncheck.check_ok(bad), bad)

    def test_frozen_record_keeps_the_check(self):
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        (t / "AGENT_CHECKS.json").write_text(json.dumps({"checks": [
            {"name": "tests", "cmd": "make test"}, dict(self.GOOD, name="screen")]}))
        m = {}
        upgrades.freeze_checks(m, t)
        self.assertEqual([c["type"] if "type" in c else "command" for c in m["checks"]], ["command", "vision"])
        self.assertTrue(upgrades.checks_ok(m["checks"]))
        self.assertEqual(m["checks_source"], "AGENT_CHECKS.json at job start")
        got = {c.name: c for c in upgrades.job_checks(m)}
        self.assertEqual(got["screen"].type, "vision")
        self.assertEqual(got["screen"].extra["open"], "index.html")
        self.assertEqual(got["screen"].extra["steps"], self.GOOD["steps"])
        self.assertEqual(got["tests"].type, "command")
        self.assertEqual(got["tests"].extra, {})

    def test_a_broken_vision_entry_is_dropped_not_frozen(self):
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        (t / "AGENT_CHECKS.json").write_text(json.dumps({"checks": [{"name": "screen", "type": "vision",
                                                                     "open": "index.html", "spec": ""}]}))
        m = {}
        upgrades.freeze_checks(m, t)
        self.assertNotIn("checks", m)

    def test_command_checks_are_still_checked_as_before(self):
        self.assertFalse(upgrades.checks_ok([{"name": "x", "cmd": ""}]))
        self.assertFalse(upgrades.checks_ok([{"name": "x", "cmd": "a", "timeout_s": "9"}]))
        self.assertFalse(upgrades.checks_ok([{"name": "x", "cmd": "a", "type": "vision"}]))
        self.assertFalse(upgrades.checks_ok([{"name": "x", "cmd": "a", "type": "other"}]))
        self.assertTrue(upgrades.checks_ok([{"name": "x", "cmd": "a"}]))


# ------------------------------------------------------------------ the gate and run_all

class Gate(unittest.TestCase):
    def test_a_command_check_and_a_vision_check_together(self):
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        proj = t / "proj"
        (proj / "tests").mkdir(parents=True)
        (proj / "tests" / "test_ok.py").write_text("import unittest\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n")
        checks = [ck.Check("tests", "%s -m unittest discover -s tests" % sys.executable), vcheck()]
        runners = visioncheck.runners(capture_fn=capture_shots(("final", small_png())),
                                      call=lambda body: '{"pass": false, "issues": ["no score"]}')
        summ = ck.run_all(proj, t / "out", wrap=lambda p, c: ["bash", "-lc", c], checks=checks,
                          extra_runners=runners)
        self.assertEqual([r["name"] for r in summ["results"]], ["tests", "looks right"])
        self.assertEqual(summ["results"][0]["passed"], 1)
        self.assertEqual(summ["status"], "passed")            # advisory: shown, never fails the job
        self.assertEqual(summ["results"][1]["verdict"], "fail")
        colour, text = ck.banner_text(summ)
        self.assertEqual((colour, text), ("green", "Checks passed (1 tests)"))

    def test_a_required_vision_check_fails_the_job(self):
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        checks = [vcheck(name="the screen", mode="required")]
        runners = visioncheck.runners(capture_fn=capture_shots(("final", small_png())),
                                      call=lambda body: '{"pass": false, "issues": ["no score", "no orb"]}')
        summ = ck.run_all(t, t / "out", checks=checks, extra_runners=runners)
        self.assertEqual(summ["status"], "failed")
        self.assertIn("the screen", ck.banner_text(summ)[1])
        prompt = vf.build_fix_prompt("Build the game", summ, 1, 3)
        self.assertIn("Problems seen on screen:", prompt)
        self.assertIn("no score", prompt)
        self.assertIn("the screen", prompt)

    def test_no_runner_means_the_vision_check_is_left_out(self):
        t = Path(tempfile.mkdtemp(prefix="u06a-"))
        summ = ck.run_all(t, t / "out", checks=[vcheck()])
        self.assertEqual(summ["results"], [])
        self.assertEqual(summ["status"], "unverified")

    def test_the_page_shows_the_verdict_issues_and_pictures(self):
        m = {"checks": [{"name": "screen", "type": "vision", "open": "index.html", "spec": "a score"}],
             "verify": {"rounds": [{"round": 0, "summary": {"status": "failed", "results": [
                 {"name": "looks right", "cmd": "vision: index.html", "type": "vision", "mode": "required",
                  "ok": False, "verdict": "fail", "issues": ["no score visible", "button <gone>"],
                  "screenshots": ["/jobs/j1/checks/round-0/vision-looks_right-1-final.png"],
                  "seconds": 4.2, "url": "http://127.0.0.1:8000/index.html"}]}}]}}
        h = upgrades.checks_card_html(m, "j1", True)
        self.assertIn("screenshot of index.html", h)                   # no "cmd" -> no KeyError
        self.assertIn("does not look right", h)
        self.assertIn("no score visible", h)
        self.assertIn("&lt;gone&gt;", h)                               # the issues are escaped
        self.assertIn('src="/api/shot/j1?src=round0&amp;n=0&amp;i=0"', h)
        self.assertIn('class="shot"', h)
        self.assertNotIn('&i=1"', h)                                      # only one screenshot
        self.assertIn("required", h)                                     # the mode is shown

    def test_the_bundle_lists_the_pictures_and_the_issues(self):
        m = {"verify": {"status": "failed", "banner": {"colour": "red", "text": "Checks failed: looks right"},
                        "rounds": [{"round": 0, "summary": {"status": "failed", "results": [
                            {"name": "looks right", "type": "vision", "cmd": "vision", "mode": "advisory",
                             "ok": False, "verdict": "fail", "issues": ["no score"],
                             "screenshots": ["/x/vision-looks_right-1-final.png"]}]}}]}}
        md = upgrades.bundle_section(m)
        self.assertIn("screen check `looks right`: fail (advisory)", md)
        self.assertIn("seen on screen: no score", md)
        self.assertIn("screenshot: vision-looks_right-1-final.png", md)

    def test_badge_for_an_unsure_check(self):
        m = {"verify": {"rounds": [{"round": 0, "summary": {"status": "unverified", "results": [
            {"name": "looks right", "type": "vision", "ok": False, "verdict": "inconclusive"}]}}]}}
        self.assertEqual(badges.badges(m, "j1")[0]["text"], "looks right (not sure)")


# ------------------------------------------------------------------ the screenshot endpoint

class ShotsEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="u06a-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PORT)
        jobboard.JOBS = cls.tmp / "jobs"
        jobboard.WORK = cls.tmp / "work"
        (jobboard.WORK / "proj").mkdir(parents=True)
        cls.jid = "20261111-120000-ab12"
        shots = jobboard.JOBS / cls.jid / "checks" / "round-0"
        shots.mkdir(parents=True)
        (shots / "vision-looks_right-1-final.png").write_bytes(small_png())
        outside = cls.tmp / "elsewhere.png"
        outside.write_bytes(small_png())
        jobboard.save_meta({"id": cls.jid, "job": "make it look right", "folder": "proj", "mode": "local-qwen",
                            "status": "done", "created": 1.0, "verify": {"status": "failed", "rounds": [
                                {"round": 0, "summary": {"status": "failed", "results": [
                                    {"name": "looks right", "type": "vision", "ok": False, "verdict": "fail",
                                     "screenshots": [str(shots / "vision-looks_right-1-final.png"), str(outside)]}]}}]}},
                           cls.jid)
        s = socket.socket(); s.bind(("127.0.0.1", 0)); cls.port = s.getsockname()[1]; s.close()
        jobboard.PORT = cls.port
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.thr.join(timeout=5)
        jobboard.JOBS, jobboard.WORK, jobboard.PORT = cls.saved

    def get(self, path, cookie=True):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path))
        if cookie:
            req.add_header("Cookie", "jb=%s" % jobboard.TOKEN)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_the_screenshot_is_served(self):
        code, ctype, body = self.get(f"/api/shot/{self.jid}?src=round0&n=0&i=0")
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "image/png")
        self.assertEqual(visioncheck.png_size(body), (40, 20))

    def test_only_inside_the_job_folder_and_only_png(self):
        self.assertEqual(self.get(f"/api/shot/{self.jid}?src=round0&n=0&i=1")[0], 404)   # outside JOBS/<id>
        self.assertEqual(self.get(f"/api/shot/{self.jid}?src=round0&n=0&i=9")[0], 404)
        self.assertEqual(self.get(f"/api/shot/{self.jid}?src=round7&n=0&i=0")[0], 404)
        self.assertEqual(self.get(f"/api/shot/{self.jid}")[0], 404)
        self.assertEqual(self.get("/api/shot/no-such-job?src=round0&n=0&i=0")[0], 404)

    def test_it_needs_the_login_cookie(self):
        self.assertIn(self.get(f"/api/shot/{self.jid}?src=round0&n=0&i=0", cookie=False)[0], (401, 403))


if __name__ == "__main__":
    unittest.main()
