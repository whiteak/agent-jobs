#!/usr/bin/env python3
"""Security tests: the job records moved OUT of the sandbox (default JOBS is
~/agent-sandbox/state/jobs, no longer ~/agent-work/.jobs which the sandbox
mounts), and the worker re-checks every queued record right before running it
(check_record inside process_job), because records are plain files and a
check at POST time is not enough.

All refusal tests call jobboard.process_job directly with subprocess.Popen
mocked, so a refused record must never start an agent command.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if "jobboard" not in sys.modules:
    # test_jobstatus owns the isolated-env import of jobboard (its ServerSmoke
    # pages depend on the module being imported with ITS temp folders). This
    # file sorts before it alphabetically, so hand the import over to it.
    sys.path.insert(0, str(ROOT / "tests"))
    import test_jobstatus  # noqa: F401,E402
import jobboard  # noqa: E402

JID = "20261110-081500-ab12"  # a hand-written id in the board's own format


def base_meta(**over):
    m = {"id": JID, "kind": "agent", "mode": "local-claude", "folder": "proj",
         "job": "do the thing", "offline": False, "status": "queued",
         "created": 1.0, "started": None, "finished": None, "exit": None,
         "resume": None, "parent": None, "session": None}
    m.update(over)
    return m


class DefaultJobsPath(unittest.TestCase):
    """The default JOBS must live where the sandbox cannot write it: under
    agent-sandbox/state/jobs, NOT under WORK (which the sandbox mounts rw)."""

    def test_default_jobs_is_under_agent_sandbox_state_not_work(self):
        home = Path(tempfile.mkdtemp(prefix="jbdef-"))
        env = dict(os.environ, HOME=str(home), JOBBOARD_READONLY="1")
        for k in ("JOBBOARD_JOBS", "JOBBOARD_WORK"):   # want the real defaults
            env.pop(k, None)
        r = subprocess.run(
            [sys.executable, "-c", "import jobboard; print(jobboard.JOBS); print(jobboard.WORK)"],
            env=env, capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        jobs, work = r.stdout.split()
        self.assertEqual(jobs, str(home / "agent-sandbox" / "state" / "jobs"))
        self.assertEqual(work, str(home / "agent-work"))
        self.assertFalse(jobs.startswith(work + os.sep))   # NOT inside the mounted WORK
        self.assertTrue((home / "agent-sandbox" / "state" / "jobs").is_dir())

    def test_jobs_override_still_works(self):
        home = Path(tempfile.mkdtemp(prefix="jbdef-"))
        d = str(home / "someplace")
        r = subprocess.run(
            [sys.executable, "-c", "import jobboard; print(jobboard.JOBS)"],
            env=dict(os.environ, HOME=str(home), JOBBOARD_JOBS=d,
                     JOBBOARD_WORK=str(home / "work")),
            capture_output=True, text=True, cwd=str(ROOT), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), d)


class ProcessJobRefusals(unittest.TestCase):
    """Hand-written queued records: the worker re-checks each one right before
    running it and refuses anything that fails, with the reason on the record."""

    def setUp(self):
        self.saved = (jobboard.JOBS, jobboard.WORK, jobboard.BENCH_RUNS)
        self.tmp = Path(tempfile.mkdtemp(prefix="jbsec-"))
        jobboard.JOBS = self.tmp / "jobs"; jobboard.JOBS.mkdir()
        jobboard.WORK = self.tmp / "work"; jobboard.WORK.mkdir()
        jobboard.BENCH_RUNS = self.tmp / "runs"; jobboard.BENCH_RUNS.mkdir()

    def tearDown(self):
        (jobboard.JOBS, jobboard.WORK, jobboard.BENCH_RUNS) = self.saved

    def write_record(self, dirname, meta):
        d = jobboard.job_dir(dirname)
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return dirname

    def run_one(self, dirname):
        """process_job with every outside effect mocked; returns (ran, Popen mock)."""
        with mock.patch.object(jobboard.subprocess, "Popen") as popen, \
             mock.patch.object(jobboard, "record_git_start", lambda m: None), \
             mock.patch.object(jobboard, "wait_with_watchdog", lambda jid, p: (0, None)), \
             mock.patch.object(jobboard, "notify_finished", lambda m, ok: None), \
             mock.patch.object(jobboard, "build_review_md", lambda m: None), \
             mock.patch.object(jobboard.jailcfg, "selftest", lambda force=False: (True, "OK  (mocked)")):
            ran = jobboard.process_job(dirname)
        return ran, popen

    def assert_refused(self, dirname, field):
        ran, popen = self.run_one(dirname)
        self.assertFalse(ran, "the worker must not run a refused record")
        self.assertEqual(popen.call_count, 0, "Popen must never be called for a refused record")
        m = jobboard.load_meta(dirname)
        self.assertEqual(m["status"], "failed")
        self.assertEqual(m.get("fail_note"),
                         f"Refused: the job record failed the safety check ({field})")

    def test_supervised_mode_with_bad_folder_is_refused(self):
        # the exact attack: a forged record asking for Jason's cloud account,
        # in a folder outside the projects
        self.assert_refused(self.write_record(
            JID, base_meta(mode="supervised", folder="../x")), "folder")

    def test_unknown_mode_is_refused(self):
        self.assert_refused(self.write_record(JID, base_meta(mode="wat")), "mode")

    def test_id_field_differing_from_directory_is_refused(self):
        d = self.write_record(JID, base_meta(id="20261110-081500-9999"))
        self.assert_refused(d, "id")

    def test_bad_directory_name_is_refused(self):
        self.assert_refused(self.write_record("not-an-id", base_meta(id="not-an-id")), "id")

    def test_bad_resume_value_is_refused(self):
        self.assert_refused(self.write_record(JID, base_meta(resume="--evil flag")), "resume")

    def test_huge_job_text_is_refused(self):
        self.assert_refused(self.write_record(JID, base_meta(job="x" * 300000)), "job")

    def test_offline_must_be_a_bool(self):
        self.assert_refused(self.write_record(JID, base_meta(offline="yes")), "offline")

    def test_parent_must_be_an_existing_record(self):
        self.assert_refused(self.write_record(JID, base_meta(parent="20260101-000000-ffff")), "parent")
        other = "20261110-081500-cd34"
        self.assert_refused(self.write_record(other, base_meta(id=other, parent="../x")), "parent")

    def test_valid_record_runs_with_the_expected_command(self):
        self.write_record(JID, base_meta())
        ran, popen = self.run_one(JID)
        self.assertTrue(ran)
        self.assertEqual(popen.call_count, 1)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd, [str(jobboard.AGENT), "local-claude", "--stream",
                               "--name", "agentjob-" + JID, "--in", "proj", "do the thing"])
        m = jobboard.load_meta(JID)
        self.assertEqual(m["status"], "done")
        self.assertNotIn("fail_note", m)

    def test_valid_record_with_offline_and_resume(self):
        self.write_record(JID, base_meta(offline=True, resume="abc12345-6789"))
        ran, popen = self.run_one(JID)
        self.assertTrue(ran)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd, [str(jobboard.AGENT), "local-claude", "--offline",
                               "--stream", "--name", "agentjob-" + JID, "--in", "proj",
                               "--resume", "abc12345-6789", "do the thing"])

    # ------------------------------------------------------------ bench records

    def make_bench(self, dirname=JID, mode="bench", **over):
        m = base_meta(kind="bench", mode=mode, folder="", job="a run", **over)
        run = jobboard.BENCH_RUNS / dirname
        run.mkdir(parents=True)
        (run / "spec.json").write_text(json.dumps({"id": dirname, "tasks": [], "samples": 1}),
                                       encoding="utf-8")
        return self.write_record(dirname, m)

    def test_bench_record_needs_the_spec(self):
        d = self.make_bench()
        os.remove(jobboard.BENCH_RUNS / d / "spec.json")
        ran, popen = self.run_one(d)
        self.assertFalse(ran)
        self.assertEqual(popen.call_count, 0)
        m = jobboard.load_meta(d)
        self.assertEqual(m["status"], "failed")
        self.assertEqual(m.get("fail_note"),
                         "Refused: the job record failed the safety check (spec)")

    def test_bench_record_mode_must_be_bench(self):
        self.assert_refused(self.make_bench(mode="supervised"), "mode")

    # ------------------------------------------------------------ check_record direct

    def test_check_record_table(self):
        self.assertIsNone(jobboard.check_record(JID, base_meta()))
        self.assertIsNone(jobboard.check_record(JID, base_meta(folder="a.b_c-d")))
        self.assertIsNone(jobboard.check_record(JID, base_meta(resume="abcd1234")))
        self.assertEqual(jobboard.check_record(JID, base_meta(folder="eval")), "folder")
        self.assertEqual(jobboard.check_record(JID, base_meta(folder="")), "folder")
        self.assertEqual(jobboard.check_record(JID, base_meta(job="")), "job")
        self.assertEqual(jobboard.check_record(JID, base_meta(job=None)), "job")

    def test_resume_bench_refuses_a_tampered_record(self):
        m = base_meta(kind="bench", mode="supervised", status="stopped")
        d = self.write_record(JID, m)
        (jobboard.BENCH_RUNS / d).mkdir(parents=True, exist_ok=True)
        (jobboard.BENCH_RUNS / d / "spec.json").write_text("{}", encoding="utf-8")
        self.assertIsNone(jobboard.resume_bench(d))
        self.assertEqual(jobboard.load_meta(d)["status"], "stopped")   # stays put


class StartupNotice(unittest.TestCase):
    """The board never moves the old WORK/.jobs records itself, but says one
    clear line when they are still there and the new folder is empty."""

    def setUp(self):
        self.saved = (jobboard.JOBS, jobboard.WORK)
        self.tmp = Path(tempfile.mkdtemp(prefix="jbnotice-"))
        jobboard.WORK = self.tmp / "work"; jobboard.WORK.mkdir()
        jobboard.JOBS = self.tmp / "state" / "jobs"; jobboard.JOBS.mkdir(parents=True)

    def tearDown(self):
        (jobboard.JOBS, jobboard.WORK) = self.saved

    def notice(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            printed = jobboard.old_jobs_notice()
        return printed, buf.getvalue()

    def test_prints_one_clear_line(self):
        old = jobboard.WORK / ".jobs" / JID
        old.mkdir(parents=True)
        (old / "meta.json").write_text(json.dumps(base_meta()), encoding="utf-8")
        printed, out = self.notice()
        self.assertTrue(printed)
        self.assertEqual(out.count("\n"), 1)                 # one line, no noise
        self.assertIn(str(jobboard.WORK / ".jobs"), out)     # says the old place
        self.assertIn(str(jobboard.JOBS), out)               # and the new one

    def test_silent_when_new_folder_already_has_records(self):
        (jobboard.WORK / ".jobs" / JID).mkdir(parents=True)
        (jobboard.JOBS / JID).mkdir()
        printed, out = self.notice()
        self.assertFalse(printed)
        self.assertEqual(out, "")

    def test_silent_when_old_folder_missing_or_empty(self):
        printed, out = self.notice()                      # no .jobs at all
        self.assertFalse(printed); self.assertEqual(out, "")
        (jobboard.WORK / ".jobs").mkdir()                 # empty .jobs
        printed, out = self.notice()
        self.assertFalse(printed); self.assertEqual(out, "")
        (jobboard.WORK / ".jobs" / ".trash").mkdir()      # trash is not records
        printed, out = self.notice()
        self.assertFalse(printed); self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
