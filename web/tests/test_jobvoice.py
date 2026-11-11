#!/usr/bin/env python3
"""Voice routes in jobboard.py (VOICE, 2026-11-11).

Starts a REAL read-only board process on a free port with temp folders, logs in with
its token, and checks the six voice routes, the page hooks (data-voice attributes and
the voice.js script tag), the login/origin guard on voice routes, size limits and the
jobstatus -> voice status mapping.

Speech uses the REAL installed models in ~/agent-sandbox/voice (AGENT_VOICE_DIR);
those two tests are skipped when the models are not installed. Nothing here touches
Basalt or the GPU (whisper runs on the CPU).
"""
import http.client
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICE_DIR = os.environ.get("AGENT_VOICE_DIR") or os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "agent-sandbox", "voice")  # real home: other test modules change HOME
HAVE_MODELS = (os.path.exists(os.path.join(VOICE_DIR, ".venv", "bin", "python"))
               and os.path.isdir(os.path.join(VOICE_DIR, "models", "whisper-small.en")))
SMOKE_WAV = os.path.join(VOICE_DIR, "smoke_test.wav")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def write_job(jobs, jid, **kw):
    m = {"id": jid, "mode": "local-qwen", "folder": "vproj", "job": "Add a dark mode toggle",
         "offline": False, "status": "done", "created": time.time() - 300, "started": time.time() - 290,
         "finished": time.time() - 10, "exit": 0}
    m.update(kw)
    os.makedirs(os.path.join(jobs, jid), exist_ok=True)
    with open(os.path.join(jobs, jid, "meta.json"), "w") as f:
        json.dump(m, f)
    return m


class VoiceRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="jbvoice-")
        cls.home, cls.jobs, cls.work = (os.path.join(cls.tmp, d) for d in ("home", "jobs", "work"))
        for d in (cls.home, cls.jobs, cls.work):
            os.makedirs(d)
        os.makedirs(os.path.join(cls.work, "vproj"))
        with open(os.path.join(cls.work, "vproj", "AGENT_REPORT.md"), "w") as f:
            f.write("# Report\n\n## Summary\nDark mode added. All **20** tests pass.\n\n## Details\nlots\n")
        write_job(cls.jobs, "20261111-100000-aaaa", session="abcdef12-3456")
        cls.port = free_port()
        env = dict(os.environ, HOME=cls.home, JOBBOARD_JOBS=cls.jobs, JOBBOARD_WORK=cls.work,
                   JOBBOARD_READONLY="1", JOBBOARD_PORT=str(cls.port), AGENT_VOICE_DIR=VOICE_DIR)
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "jobboard.py")], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.token = None
        deadline = time.time() + 20
        while time.time() < deadline and cls.token is None:
            line = cls.proc.stdout.readline()
            if "?t=" in line:
                cls.token = line.strip().split("?t=")[1]
        assert cls.token, "board did not start"
        cls.cookie = "jb=" + cls.token

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try: cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired: cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def req(self, method, path, body=None, headers=None, cookie=True, timeout=60):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Host": f"127.0.0.1:{self.port}"}
        if cookie: h["Cookie"] = self.cookie
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse(); data = r.read(); c.close()
        return r.status, r.getheader("Content-Type") or "", data

    def jpost(self, path, obj):
        return self.req("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json",
                                                                  "Origin": f"http://127.0.0.1:{self.port}"})

    # ---- page hooks
    def test_pages_have_voice_hooks(self):
        st, _, b = self.req("GET", "/")
        self.assertEqual(st, 200)
        page = b.decode()
        self.assertIn('<script src="/static/voice.js"></script></body>', page)
        self.assertIn('data-voice="new-job"', page)
        self.assertIn('<meta charset="utf-8">', page)
        st, _, b = self.req("GET", "/job/20261111-100000-aaaa")
        page = b.decode()
        self.assertIn('data-voice="follow-up" data-job="20261111-100000-aaaa"', page)
        self.assertIn("<pre data-voice-read>", page)
        self.assertIn("/static/voice.js", page)

    def test_static_voice_js(self):
        st, ct, b = self.req("GET", "/static/voice.js")
        self.assertEqual(st, 200)
        self.assertIn("javascript", ct)
        self.assertIn(b"window.AgentVoice", b)
        if shutil.which("node"):
            with tempfile.NamedTemporaryFile("wb", suffix=".js", delete=False) as f:
                f.write(b); path = f.name
            r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
            os.unlink(path)
            self.assertEqual(r.returncode, 0, r.stderr)

    # ---- guard
    def test_voice_routes_need_login(self):
        for m, p in (("GET", "/static/voice.js"), ("GET", "/api/voice/status"), ("GET", "/api/voice/report"),
                     ("POST", "/api/transcribe"), ("POST", "/api/speak"), ("POST", "/api/voice/command")):
            st, _, _ = self.req(m, p, b"{}" if m == "POST" else None, cookie=False)
            self.assertEqual(st, 403, p)

    def test_voice_post_refuses_other_origins(self):
        st, _, _ = self.req("POST", "/api/voice/command", b'{"text":"agent status"}',
                            {"Content-Type": "application/json", "Origin": "http://evil.example"})
        self.assertEqual(st, 403)

    # ---- status / report / command
    def test_status_finished_uses_fresh_report(self):
        st, ct, b = self.req("GET", "/api/voice/status")
        self.assertEqual(st, 200)
        j = json.loads(b)
        self.assertEqual(j["job_id"], "20261111-100000-aaaa")
        self.assertEqual(j["status"], "finished")
        self.assertEqual(j["text"], "Job finished. Add a dark mode toggle. Dark mode added. All 20 tests pass.")

    def test_report_is_clean_text(self):
        j = json.loads(self.req("GET", "/api/voice/report")[2])
        self.assertIn("Dark mode added. All 20 tests pass.", j["text"])
        self.assertNotIn("#", j["text"]); self.assertNotIn("**", j["text"])

    def test_command(self):
        st, _, b = self.jpost("/api/voice/command", {"text": "Agent, new job: write a readme."})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(b), {"command": {"cmd": "new_job", "text": "write a readme"}})
        self.assertEqual(json.loads(self.jpost("/api/voice/command", {"text": "hello there"})[2]), {"command": None})
        st, _, b = self.req("POST", "/api/voice/command", b"not json", {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 400)

    def test_limits_and_errors(self):
        st, _, b = self.req("POST", "/api/transcribe", b"", {"Content-Length": str(26 * 1024 * 1024),
                                                            "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 413)
        st, _, b = self.req("POST", "/api/transcribe", b"", {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 400); self.assertIn("Empty", json.loads(b)["error"])
        st, _, b = self.jpost("/api/speak", {"text": "  ** "})
        self.assertEqual(st, 400); self.assertIn("Nothing", json.loads(b)["error"])

    # ---- real models (CPU)
    @unittest.skipUnless(HAVE_MODELS, "voice models not installed")
    def test_speak_real_piper(self):
        st, ct, b = self.jpost("/api/speak", {"text": "Job finished. Dark mode added."})
        self.assertEqual(st, 200); self.assertEqual(ct, "audio/wav")
        self.assertTrue(b.startswith(b"RIFF") and len(b) > 20000)

    @unittest.skipUnless(HAVE_MODELS and os.path.exists(SMOKE_WAV), "voice models not installed")
    def test_transcribe_real_whisper(self):
        with open(SMOKE_WAV, "rb") as f:
            audio = f.read()
        st, _, b = self.req("POST", "/api/transcribe", audio, {"Content-Type": "audio/wav",
                                                               "Origin": f"http://127.0.0.1:{self.port}"}, timeout=120)
        self.assertEqual(st, 200, b)
        text = json.loads(b)["text"].lower()
        for w in ("voice check", "android app", "run the tests"):
            self.assertIn(w, text)


class VoiceStatusMapping(unittest.TestCase):
    """voice_status() maps jobstatus banner states onto queued/running/finished/failed/stuck/stopped."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jbvmap-")
        sys.path.insert(0, ROOT)
        if "jobboard" not in sys.modules:   # never let an in-process import touch the real ~/agent-sandbox
            os.makedirs(os.path.join(self.tmp, "home"))
            os.environ.update(HOME=os.path.join(self.tmp, "home"), JOBBOARD_READONLY="1",
                              JOBBOARD_JOBS=os.path.join(self.tmp, "j0"), JOBBOARD_WORK=os.path.join(self.tmp, "w0"),
                              AGENT_VOICE_DIR=VOICE_DIR)
        import jobboard
        self.jb = jobboard
        self.old = (jobboard.JOBS, jobboard.WORK)
        jobboard.JOBS = os.path.join(self.tmp, "jobs"); jobboard.WORK = os.path.join(self.tmp, "work")
        from pathlib import Path
        jobboard.JOBS, jobboard.WORK = Path(jobboard.JOBS), Path(jobboard.WORK)
        jobboard.JOBS.mkdir(); (jobboard.WORK / "vproj").mkdir(parents=True)

    def tearDown(self):
        self.jb.JOBS, self.jb.WORK = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mapping(self):
        jb = self.jb
        self.assertEqual(jb.voice_status(), {"job_id": None, "status": None, "text": "There are no jobs yet."})
        write_job(str(jb.JOBS), "20261111-100000-aaaa", status="queued", started=None, finished=None)
        j = jb.voice_status()
        self.assertEqual(j["status"], "queued"); self.assertTrue(j["text"].startswith("Job queued. Add a dark mode toggle."))
        write_job(str(jb.JOBS), "20261111-100000-aaaa", status="failed", exit=1, fail_note="Build failed")
        self.assertEqual(jb.voice_status()["status"], "failed")
        write_job(str(jb.JOBS), "20261111-100000-aaaa", status="stopped", exit=143)
        self.assertEqual(jb.voice_status()["status"], "stopped")
        write_job(str(jb.JOBS), "20261111-100000-aaaa", status="running", finished=None, exit=None)
        self.assertIn(jb.voice_status()["status"], ("running", "stuck"))
        # an old report in the folder is not read out as this job's result
        rp = jb.WORK / "vproj" / "AGENT_REPORT.md"
        rp.write_text("## Summary\nOld result.")
        os.utime(rp, (time.time() - 3600, time.time() - 3600))
        write_job(str(jb.JOBS), "20261111-100000-aaaa", status="done")
        self.assertNotIn("Old result", jb.voice_status()["text"])
        # bench runs are skipped: the newest AGENT job is reported
        write_job(str(jb.JOBS), "20261111-110000-bbbb", kind="bench", mode="bench", folder="", job="bench run")
        self.assertEqual(jb.voice_status()["job_id"], "20261111-100000-aaaa")


if __name__ == "__main__":
    unittest.main()
