#!/usr/bin/env python3
"""Job D: the Chat page (web/chat.py + the "# CHAT" hooks in jobboard.py).

What is pinned here:

  * a chat turn runs the AGENT the way a job does - the same argv (local-qwen, --offline,
    --stream, --in <folder>, --name agentjob-<turn id>, --resume), the same check_record
    re-check right before it runs, the same jail self-test and the same watchdog (a forbidden
    command safety-stops the turn), and the board's own Stop path ends a turn;
  * one message = one agent process, and every message after the first carries --resume of the
    session the first turn left, so it is one continuing conversation;
  * no verify gate and no checkpoints: a chat turn never becomes a job record, never lands in
    the job queue, and leaves no verify/checkpoint state behind;
  * the conversation lives in state/chats/<id>.json (folder, session id, messages with time) and
    reloads; each turn's raw stream lives under the same folder;
  * the live rows: assistant text, one line per tool call, and a tool's OUTPUT never shown;
  * talk-back text: the agent's own words, code and tool output gone, cut at ~600 characters
    with "the rest is on screen";
  * hands-free on /chat: "agent X" sends X, a bare "agent" wakes, "agent stop" stops the talking
    and "agent cancel" stops the turn;
  * the page escapes everything, needs the same login as every other page (a logged-out phone
    gets 303 for GET and 403 for POST), refuses a POST from another Origin, refuses everything
    on a read-only board, and the Chat link is in the menu.

The agent is always the fake agent (JOBBOARD_FAKE mode + JOBBOARD_JAIL=host), so no model, no
podman and no Basalt. A "phone" is stood in by patching H.remote (this sandbox has one interface).
Nothing here contacts port 8080 or port 8090.
"""
import ast
import http.server
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if "jobboard" not in sys.modules:
    # test_jobstatus owns the isolated-env import of jobboard (see test_jobsecurity.py); this
    # file sorts before both, so hand the import over to it instead of importing it twice.
    sys.path.insert(0, str(ROOT / "tests"))
    import test_jobstatus  # noqa: F401,E402
import jobboard  # noqa: E402
import chat  # noqa: E402
import phonelayout  # noqa: E402
import upgrades  # noqa: E402
import voice_core  # noqa: E402

PHONE = "192.0.2.55"          # TEST-NET-1: stands in for a phone through H.remote
PW = "board pass word"
FAST = 100                    # PBKDF2 rounds for the test password
BAD_CMD = "curl https://evil.example.com/x"    # what the watchdog must stop


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)   # the board answers a POST with a 303: see it


def parse_sse(chunks):
    """[(event, data)] out of SSE text chunks (test_u07_dashboard's parser, without ids)."""
    out = []
    for block in "".join(chunks).split("\n\n"):
        ev = {"event": None, "data": None}
        for line in block.splitlines():
            if line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[6:])
        if ev["event"]:
            out.append((ev["event"], ev["data"]))
    return out


def scen(*steps):
    """A fake-agent scenario (the fake mode's job text): one round, these steps."""
    return json.dumps({"rounds": [list(steps)]})


class Turn(unittest.TestCase):
    """Chat turns for real: the fake agent as a child process, in temp folders, host jail."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chat-turn-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = self.tmp / "work"
        self.proj = self.work / "proj"
        self.proj.mkdir(parents=True)
        (self.proj / "PLAN.md").write_text("# Plan\n- [ ] one\n- [ ] two\n")
        self.proj2 = self.work / "other-app"
        self.proj2.mkdir()
        self.jobs = self.tmp / "jobs"; self.jobs.mkdir()
        self.chats = self.tmp / "chats"
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(json.dumps({"watchdog_tick_s": 1, "after_result_grace_s": 2}))
        saved = (jobboard.WORK, jobboard.JOBS, jobboard.CHATS, jobboard.SETTINGS_FILE)
        def restore():
            jobboard.WORK, jobboard.JOBS, jobboard.CHATS, jobboard.SETTINGS_FILE = saved
        self.addCleanup(restore)
        jobboard.WORK, jobboard.JOBS = self.work, self.jobs
        jobboard.CHATS, jobboard.SETTINGS_FILE = self.chats, self.settings
        for patch in (mock.patch.object(upgrades, "HOST_MODE", True),               # no podman
                      mock.patch.dict(jobboard.MODES, {upgrades.FAKE_MODE: "Fake agent (tests only)"}),
                      mock.patch.object(chat, "CHAT_MODE", upgrades.FAKE_MODE)):
            patch.start()
            self.addCleanup(patch.stop)

    # helpers
    def ctx(self):
        return jobboard.chat_ctx()

    def new(self, folder="proj"):
        return chat.new_chat(jobboard.CHATS, folder, upgrades.FAKE_MODE)

    def send(self, cid, text):
        """Start the turn and wait for it: the same call the POST route makes."""
        tid, note = chat.start_turn(self.ctx(), cid, text)
        self.assertEqual(note, "")
        return self.wait(tid)

    def wait(self, tid, timeout=60):
        """Poll the record until that turn is over (turns run on their own thread)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for rec in chat.list_chats(jobboard.CHATS):
                t = rec.get("turn") or {}
                if t.get("id") == tid and t.get("state") not in chat.ACTIVE:
                    return rec
            time.sleep(0.1)
        self.fail(f"turn {tid} never finished")

    def log_rows(self, cid):
        return chat.turn_items(self.ctx(), jobboard.CHATS, cid, 0)[0]

    # ---------------------------------------------------------------- the conversation file
    def test_a_conversation_is_saved_in_state_chats_and_reloads(self):
        rec = self.new()
        self.assertEqual(chat.chat_file(jobboard.CHATS, rec["id"]),
                         self.chats / (rec["id"] + ".json"))
        self.assertTrue((self.chats / (rec["id"] + ".json")).is_file())
        rec2 = self.send(rec["id"], scen({"say": "the sky is blue"}))
        raw = json.loads((self.chats / (rec2["id"] + ".json")).read_text())
        self.assertEqual(raw["folder"], "proj")
        self.assertEqual(raw["mode"], upgrades.FAKE_MODE)
        self.assertTrue(raw["session"], "the session id of the first turn is saved")
        self.assertEqual([m["role"] for m in raw["messages"]], ["you", "agent"])
        self.assertIn("the sky is blue", raw["messages"][1]["text"])
        for m in raw["messages"]:
            self.assertIsInstance(m["time"], float)
        # it reloads, and the list shows it
        again = chat.load(jobboard.CHATS, rec["id"])
        self.assertEqual(again, raw)
        self.assertEqual([c["id"] for c in chat.list_chats(jobboard.CHATS)], [rec["id"]])
        # ... and the turn's own raw stream sits next to it
        log = chat.turn_log(jobboard.CHATS, rec["id"], raw["turn"]["id"])
        self.assertTrue(log.is_file() and log.stat().st_size > 0)

    def test_the_first_message_starts_a_session_and_the_second_resumes_it(self):
        rec = self.new()
        first = self.send(rec["id"], scen({"say": "first answer"}))
        sid = first["session"]
        self.assertTrue(jobboard.SESSION_RE.fullmatch(sid))
        first_turn = first["turn"]["id"]
        # the second turn's argv carries --resume <that session>
        second_text = scen({"say": "second answer"})
        tid2, _ = chat.start_turn(self.ctx(), rec["id"], second_text)
        rec2 = chat.load(jobboard.CHATS, rec["id"])
        turn2 = rec2["turn"]
        argv, _cwd = jobboard.agent_argv(chat.turn_meta(rec2, turn2), second_text, sid, 0)
        self.assertIn("--resume", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], sid)
        self.wait(tid2)
        rec3 = chat.load(jobboard.CHATS, rec["id"])
        # the fake agent only ever prints the session it was started with, so a stream that
        # repeats the first session id proves --resume reached it
        self.assertEqual(rec3["session"], sid)
        rows = [json.loads(l) for l in
                chat.turn_log(jobboard.CHATS, rec["id"], rec3["turn"]["id"]).read_text().splitlines()]
        self.assertEqual({r.get("session_id") for r in rows if r.get("session_id")}, {sid})
        self.assertNotEqual(rec3["turn"]["id"], first_turn)
        self.assertEqual([m["role"] for m in rec3["messages"]], ["you", "agent", "you", "agent"])
        self.assertIn("second answer", rec3["messages"][3]["text"])

    def test_a_chat_turn_is_not_a_job_and_leaves_no_gate_or_checkpoint_state(self):
        rec = self.new()
        rec2 = self.send(rec["id"], scen({"say": "answer"}))
        tid = rec2["turn"]["id"]
        self.assertIsNone(jobboard.load_meta(tid), "a chat turn is never a job record")
        self.assertFalse((self.jobs / tid).exists(), "and it never lands in the job folders")
        self.assertEqual([m["id"] for m in jobboard.all_jobs()], [])
        raw = json.loads((self.chats / (rec["id"] + ".json")).read_text())
        for key in ("verify", "checkpoints", "checkpoints_note", "checks", "fix_rounds", "fail_note"):
            self.assertNotIn(key, raw["turn"], f"{key} belongs to the verify gate, not to a chat")
        self.assertFalse((self.proj / ".git").exists(), "no checkpoints were made in the project")
        # what chat.py actually calls, read off its syntax tree (its comments are free to mention
        # the gate by name to explain why it is not used)
        called = set()
        for n in ast.walk(ast.parse((ROOT / "chat.py").read_text())):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    called.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    called.add(n.func.attr)
        for name in ("run_gate", "VerifyGate", "CheckpointManager", "make_checkpoints", "checks_for",
                     "new_job", "new_job_from_form", "process_job", "run_agent_job", "run_agent_loop",
                     "run_checks", "run_queue", "worker"):
            self.assertNotIn(name, called, f"chat.py must never call {name}")
        self.assertNotIn("timeline", "".join(sorted(called)), "a chat turn has no timeline")

    def test_the_agent_is_asked_once_per_message(self):
        rec = self.new()
        rec2 = self.send(rec["id"], scen({"say": "one"}, {"say": "two"}))
        rows = self.log_rows(rec["id"])
        self.assertEqual(sum(1 for r in rows if r["k"] == "final"), 1, "one agent run, one result line")
        self.assertEqual(rec2["turn"]["exit"], 0)

    def test_two_turns_at_once_in_one_chat_are_refused(self):
        cid = self.new()["id"]
        tid, _ = chat.start_turn(self.ctx(), cid, scen({"say": "x"}, {"sleep": 6}))
        self.addCleanup(lambda: (chat.stop_turn(self.ctx(), cid), self.wait(tid)))
        tid2, note = chat.start_turn(self.ctx(), cid, "are you there?")
        self.assertIsNone(tid2)
        self.assertIn("still answering", note)
        self.assertEqual(len(chat.load(jobboard.CHATS, cid)["messages"]), 1)

    # ---------------------------------------------------------------- the argv and the safety check
    def test_the_turn_argv_is_the_job_argv(self):
        rec = self.new()
        rec["mode"], rec["session"] = "local-qwen", "sess-12345678"
        turn = {"id": "20261112-100000-abcd", "text": "what changed today?", "state": "waiting",
                "started": time.time()}
        meta = chat.turn_meta(rec, turn)
        job = {"id": turn["id"], "kind": "chat", "mode": "local-qwen", "folder": "proj",
               "job": "what changed today?", "offline": True, "status": "waiting",
               "created": turn["started"], "started": turn["started"], "finished": None,
               "exit": None, "resume": "sess-12345678", "parent": None, "session": None}
        argv_chat, cwd_chat = jobboard.agent_argv(meta, meta["job"], "sess-12345678", 0)
        argv_job, cwd_job = jobboard.agent_argv(job, job["job"], "sess-12345678", 0)
        self.assertEqual(argv_chat, argv_job)
        self.assertEqual(cwd_chat, cwd_job)
        self.assertEqual(argv_chat[:2], [str(jobboard.AGENT), "local-qwen"])
        self.assertEqual(argv_chat[2], "--offline", "a local mode is always offline (the jail)")
        self.assertIn("--stream", argv_chat)
        self.assertIn("--in", argv_chat)
        self.assertEqual(argv_chat[argv_chat.index("--in") + 1], "proj")
        self.assertIn("agentjob-" + turn["id"], argv_chat)   # the name Stop uses
        self.assertEqual(argv_chat[-1], "what changed today?")
        self.assertEqual(argv_chat[argv_chat.index("--resume") + 1], "sess-12345678")

    def test_a_local_chat_turn_is_offline_even_if_the_record_says_otherwise(self):
        rec = self.new()
        rec["mode"], rec["offline"] = "local-qwen", False
        turn = {"id": "20261112-100000-abcd", "text": "hi", "state": "waiting"}
        self.assertTrue(chat.turn_meta(rec, turn)["offline"])
        argv, _cwd = jobboard.agent_argv(chat.turn_meta(rec, turn), "hi", None, 0)
        self.assertEqual(argv[:3], [str(jobboard.AGENT), "local-qwen", "--offline"])

    def test_a_chat_turn_passes_the_boards_own_record_check(self):
        rec = self.new()
        turn = {"id": "20261112-100000-abcd", "text": scen({"say": "hello"}), "state": "waiting",
                "started": time.time()}
        self.assertIsNone(jobboard.check_record(turn["id"], chat.turn_meta(rec, turn)))
        for folder, why in (("bad folder", "folder"), ("eval", "folder"), ("../elsewhere", "folder"),
                            ("a" * 70, "folder")):
            rec["folder"] = folder
            self.assertEqual(jobboard.check_record(turn["id"], chat.turn_meta(rec, turn)), why, folder)
        rec["folder"] = "proj"
        turn["id"] = "not-an-id"
        self.assertEqual(jobboard.check_record(turn["id"], chat.turn_meta(rec, turn)), "id")

    def test_a_turn_whose_mode_is_not_on_the_board_is_refused_and_never_runs(self):
        rec = chat.new_chat(jobboard.CHATS, "proj", "supervillain")
        tid, _ = chat.start_turn(self.ctx(), rec["id"], "hello")
        rec2 = self.wait(tid)
        self.assertEqual(rec2["turn"]["state"], "refused")
        self.assertIn("safety check (mode)", rec2["turn"]["note"])
        self.assertFalse((self.proj / ".agentjobs").exists(), "the fake agent never started")
        self.assertEqual(rec2["messages"][0]["role"], "you")   # the words are kept, the agent did not run

    def test_a_local_turn_is_refused_when_the_sandbox_self_test_fails(self):
        rec = chat.new_chat(jobboard.CHATS, "proj", "local-qwen")
        out = "ok: no internet\nFAIL: the model can be reached from outside\n"
        with mock.patch.object(jobboard.jailcfg, "selftest", lambda force=False: (False, out)):
            tid, _ = chat.start_turn(self.ctx(), rec["id"], "hello")
            rec2 = self.wait(tid)
        self.assertEqual(rec2["turn"]["state"], "refused")
        self.assertIn("Sandbox check failed", rec2["turn"]["note"])
        self.assertIn("reached from outside", rec2["turn"]["note"])

    # ---------------------------------------------------------------- the watchdog
    def test_a_forbidden_command_safety_stops_the_turn(self):
        rec = self.new()
        t0 = time.time()
        rec2 = self.send(rec["id"], scen({"say": "let me get that"}, {"cmd": BAD_CMD},
                                         {"sleep": 40}, {"say": "never said"}))
        self.assertLess(time.time() - t0, 35, "the turn was killed, not left to sleep 40 s")
        self.assertEqual(rec2["turn"]["state"], "safety_stop")
        self.assertEqual(rec2["turn"]["how"], "safety_stop")
        self.assertIn("Safety stop", rec2["turn"]["note"])
        self.assertIn("evil.example.com", rec2["turn"]["note"])
        rows = self.log_rows(rec["id"])
        self.assertTrue([r for r in rows if r["k"] == "gate" and "curl" in r["t"]],
                        "the stop is said in the live chat too")
        self.assertNotIn("never said", " ".join(m["text"] for m in rec2["messages"]
                                                if m["role"] == "agent"),
                         "the steps after the forbidden command never ran")
        self.assertEqual(rec2["messages"][-1]["text"], "let me get that")
        # a safety stop is not a job: no gate, and the session survives for the next message
        self.assertNotIn("verify", rec2["turn"])
        self.assertTrue(rec2["session"])

    def test_ordinary_commands_are_not_safety_stopped(self):
        rec = self.new()
        rec2 = self.send(rec["id"], scen({"cmd": "python3 -m unittest -q"}, {"cmd": "git commit -am x"},
                                         {"say": "done looking"}))
        self.assertEqual(rec2["turn"]["state"], "done")
        self.assertEqual(rec2["turn"]["how"], None)

    def test_stop_ends_the_current_turn(self):
        rec = self.new()
        tid, _ = chat.start_turn(self.ctx(), rec["id"], scen({"say": "working on it"}, {"sleep": 40}))
        deadline = time.time() + 30
        while time.time() < deadline:
            if (chat.turn_status(jobboard.CHATS, chat.load(jobboard.CHATS, rec["id"]))["log_size"] > 0):
                break
            time.sleep(0.1)
        self.assertEqual(chat.stop_turn(self.ctx(), rec["id"]), tid)
        rec2 = self.wait(tid)
        self.assertEqual(rec2["turn"]["state"], "stopped")
        self.assertEqual(rec2["turn"]["how"], "user_stop")
        self.assertEqual(rec2["turn"]["note"], "Stopped by you")
        self.assertLess(rec2["turn"]["seconds"], 35, "the sleep did not run to the end")
        self.assertNotIn("never", json.dumps(rec2["messages"]))
        # and the chat is free again
        self.assertIsNone(chat.stop_turn(self.ctx(), rec["id"]))
        tid2, note = chat.start_turn(self.ctx(), rec["id"], scen({"say": "back again"}))
        self.assertEqual(note, "")
        self.assertIn("back again", self.wait(tid2)["messages"][-1]["text"])

    def test_a_stopped_turn_is_not_said_to_be_an_answer(self):
        rec = self.new()
        tid, _ = chat.start_turn(self.ctx(), rec["id"], scen({"sleep": 40}))
        chat.stop_turn(self.ctx(), rec["id"])
        rec2 = self.wait(tid)
        self.assertEqual(rec2["turn"]["state"], "stopped")
        log = chat.turn_log(jobboard.CHATS, rec["id"], tid)
        rows = chat.turn_items(self.ctx(), jobboard.CHATS, rec["id"], 0)[0]
        raw = log.read_text()[:200] if log.is_file() else "(no agent log: it never started)"
        self.assertEqual([m["role"] for m in rec2["messages"]], ["you"],
                         f"nothing was answered: {json.dumps(rows)[:200]} / {raw}")

    # ---------------------------------------------------------------- the live rows
    def test_a_tool_call_is_one_line_and_its_output_is_not_shown(self):
        rec = self.new()
        rec2 = self.send(rec["id"], scen({"cmd": "pytest -q"}, {"say": "the tests pass"}))
        rows = self.log_rows(rec["id"])
        tools = [r for r in rows if r["k"] == "tool"]
        self.assertEqual(tools[0]["t"], "pytest -q")
        self.assertEqual(tools[0]["cat"], "cmd")        # the page renders this as "ran: pytest -q"
        answer = rec2["messages"][-1]["text"]
        self.assertEqual(answer, "the tests pass")      # the agent's words, not its tool rows
        raw = chat.turn_log(jobboard.CHATS, rec["id"], rec2["turn"]["id"]).read_text()
        self.assertIn("pytest -q", raw)
        # a tool's own output is in the log and never in the answer or the spoken text
        rec3 = self.send(rec["id"], scen({"cmd": "echo TOOL_OUT_MARK", "run": True}, {"say": "I looked"}))
        rows2 = self.log_rows(rec["id"])
        self.assertTrue([r for r in rows2 if r["k"] == "result" and "TOOL_OUT_MARK" in r["t"]],
                        "the raw log does hold the tool's output")
        self.assertNotIn("TOOL_OUT_MARK", rec3["messages"][-1]["text"])
        self.assertNotIn("TOOL_OUT_MARK", chat.answer_text(rows2))
        self.assertNotIn("TOOL_OUT_MARK", chat.speech_text(chat.answer_text(rows2)))

    def test_the_page_script_never_shows_a_result_row_or_raw_html(self):
        page = chat.open_chat_body(self.ctx(), chat.new_chat(jobboard.CHATS, "proj"))
        script = max(re.findall(r"<script>(.*?)</script>", page, re.S), key=len)
        self.assertIn("(function(){", script)             # the one script the page runs
        self.assertNotIn('"result"', script)          # a tool's output is not rendered
        self.assertNotIn("innerHTML", script)         # everything streamed in goes in as text
        self.assertIn('it.k==="tool"', script)
        self.assertIn('VERB={cmd:"ran: ",edit:"edited: ",read:"read: "', script)
        self.assertIn("waiting for the model", page.lower())
        self.assertIn('id="waiting"', page)

    def test_a_turn_that_has_not_answered_yet_is_waiting_for_the_model(self):
        rec = self.new()
        rec["turn"] = {"id": "20261112-100000-abcd", "text": "hello", "state": "running",
                       "started": time.time()}
        chat.save(jobboard.CHATS, rec)
        s = chat.turn_status(jobboard.CHATS, chat.load(jobboard.CHATS, rec["id"]))
        self.assertEqual((s["state"], s["label"]), ("waiting", "Waiting for the model"))
        self.assertTrue(s["can_stop"])
        log = chat.turn_log(jobboard.CHATS, rec["id"], rec["turn"]["id"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('{"type":"system","session_id":"sess-12345678"}\n')
        s2 = chat.turn_status(jobboard.CHATS, chat.load(jobboard.CHATS, rec["id"]))
        self.assertEqual(s2["state"], "running")

    # ---------------------------------------------------------------- talk-back
    def test_talk_back_is_the_agents_own_words_with_code_gone(self):
        rec = self.new()
        rec2 = self.send(rec["id"], scen({"say": "Fixed it.\n\n```python\ndef add(a, b): return a+b\n```\n"
                                          "All 12 tests pass."}, {"cmd": "pytest -q"}))
        said = chat.speech_text(chat.answer_text(self.log_rows(rec["id"])))
        self.assertIn("Fixed it", said)
        self.assertIn("All 12 tests pass", said)
        self.assertNotIn("def add", said, "a code block is never read aloud")
        self.assertNotIn("```", said)
        self.assertNotIn("pytest -q", said, "a tool call is never read aloud")

    def test_a_long_answer_is_read_as_its_start_plus_a_pointer_to_the_screen(self):
        long_text = "Short sentence. " * 200
        said = chat.speech_text(long_text)
        self.assertLess(len(said), chat.SPEECH_MAX + len(chat.SPEECH_TAIL) + 40)
        self.assertTrue(said.endswith(chat.SPEECH_TAIL))
        self.assertIn("Short sentence", said)
        self.assertNotIn("  ", said.rstrip())
        short = chat.speech_text("Two words only.")
        self.assertEqual(short, "Two words only.")
        self.assertNotIn(chat.SPEECH_TAIL, short)

    def test_turn_status_and_speech_survive_a_missing_chat(self):
        self.assertEqual(chat.turn_items(self.ctx(), jobboard.CHATS, "20261112-100000-abcd", 0), ([], 0))
        self.assertEqual(chat.answer_text([]), "")
        self.assertEqual(chat.speech_text(""), "")
        self.assertEqual(chat.stop_turn(self.ctx(), "20261112-100000-abcd"), None)

    # ---------------------------------------------------------------- ids and records
    def test_a_chat_id_is_the_shape_a_job_id_has(self):
        self.assertEqual(chat.CHAT_ID_RE.pattern, jobboard.JOB_ID_RE.pattern)
        rec = self.new()
        self.assertTrue(jobboard.JOB_ID_RE.fullmatch(rec["id"]))
        self.assertTrue(jobboard.JOB_ID_RE.fullmatch(self.send(rec["id"], scen({"say": "x"}))["turn"]["id"]))

    def test_no_id_that_is_not_an_id_becomes_a_path(self):
        chats = jobboard.CHATS
        for bad in ("", "..", "../etc/passwd", "a/b", "20261112-100000-abcd/../../x", "2026-11-12", "x" * 9):
            self.assertIsNone(chat.chat_file(chats, bad), bad)
            self.assertIsNone(chat.chat_dir(chats, bad), bad)
            self.assertIsNone(chat.load(chats, bad), bad)
            self.assertIsNone(chat.turn_log(chats, "20261112-100000-abcd", bad), bad)

    def test_a_tampered_chat_file_is_not_used(self):
        rec = self.new()
        p = chat.chat_file(jobboard.CHATS, rec["id"])
        for bad in ({"id": rec["id"], "folder": "../elsewhere", "mode": "local-qwen", "messages": []},
                    {"id": rec["id"], "folder": "eval", "mode": "local-qwen", "messages": []},
                    {"id": rec["id"], "folder": "proj", "mode": "local-qwen", "messages": [],
                     "session": "rm -rf /"},
                    {"id": "20261112-100000-zzzz", "folder": "proj", "mode": "local-qwen", "messages": []},
                    {"id": rec["id"], "folder": "proj", "mode": "local-qwen", "messages": "nope"},
                    {"id": rec["id"], "folder": "proj", "mode": "local-qwen", "messages": [],
                     "turn": {"id": "x", "text": "t", "state": "running"}}):
            p.write_text(json.dumps(bad))
            self.assertIsNone(chat.load(jobboard.CHATS, rec["id"]), json.dumps(bad)[:60])
        p.write_text("{ not json")
        self.assertIsNone(chat.load(jobboard.CHATS, rec["id"]))

    def test_the_chat_list_skips_junk_and_is_newest_first(self):
        jobboard.CHATS.mkdir(parents=True, exist_ok=True)
        a = self.new()
        b = self.new()
        c = self.new()
        (jobboard.CHATS / "junk.json").write_text("[1,2,3]")
        (jobboard.CHATS / "20260101-000000-ffff.json").write_text('{"id": "other", "folder": "x"}')
        (jobboard.CHATS / "notes.txt").write_text("hello")
        (jobboard.CHATS / b["id"]).mkdir()          # the turn-log folder of a chat is not a chat
        ids = [r["id"] for r in chat.list_chats(jobboard.CHATS)]
        self.assertEqual(ids, sorted([a["id"], b["id"], c["id"]], reverse=True),
                         "newest first, and only the real records")

    def test_a_turn_left_running_by_a_dead_board_is_said_to_be_interrupted(self):
        rec = self.new()
        rec["turn"] = {"id": "20261112-100000-abcd", "text": "hello", "state": "running",
                       "started": time.time()}
        chat.save(jobboard.CHATS, rec)
        self.assertEqual(chat.mark_interrupted(jobboard.CHATS), 1)
        rec2 = chat.load(jobboard.CHATS, rec["id"])
        self.assertEqual(rec2["turn"]["state"], "interrupted")
        self.assertIn("board stopped", rec2["turn"]["note"])
        self.assertEqual(chat.turn_status(jobboard.CHATS, rec2)["can_stop"], False)

    def test_a_stop_never_lands_on_the_next_turn(self):
        rec = self.new()
        rec["turn"] = {"id": "20261112-100000-abcd", "text": "hello", "state": "running",
                       "started": time.time()}
        chat.save(jobboard.CHATS, rec)
        self.assertIsNone(chat.patch_turn(jobboard.CHATS, rec["id"], "20261112-100000-9999",
                                          state="stopped"))
        self.assertEqual(chat.load(jobboard.CHATS, rec["id"])["turn"]["state"], "running")


class Voice(unittest.TestCase):
    """Hands-free on the Chat page: what is a command and what is the message."""

    def test_agent_x_is_the_message(self):
        for heard, want in (("agent what changed today?", "what changed today?"),
                            ("Agent, add a dark mode switch please", "add a dark mode switch please"),
                            ("agent status", "status"),                      # asks the agent, not the board
                            ("agent new job: build the app", "new job: build the app"),
                            ("hey agent run the tests", "run the tests")):
            self.assertEqual(voice_core.chat_command(heard), {"cmd": "say", "text": want}, heard)

    def test_a_bare_agent_wakes_and_stop_and_cancel_stay_commands(self):
        for heard in ("agent", "Agent.", "  okay agent? ", "hi agent,"):
            self.assertEqual(voice_core.chat_command(heard), {"cmd": "wake", "text": ""}, heard)
        for heard in ("agent stop", "agent stop talking", "agent quiet", "agent never mind",
                      "agent cancel that"):
            self.assertIn(voice_core.chat_command(heard)["cmd"], ("stop", "cancel"), heard)

    def test_a_phrase_without_the_wake_word_is_ignored(self):
        for heard in ("", "what's the weather", "  ", "not an agent"):
            self.assertIsNone(voice_core.chat_command(heard), repr(heard))

    def test_the_board_commands_are_unchanged_on_the_other_pages(self):
        self.assertEqual(voice_core.parse_command("Agent, status."), {"cmd": "status", "text": ""})
        self.assertEqual(voice_core.parse_command("agent new job: add dark mode"),
                         {"cmd": "new_job", "text": "add dark mode"})
        self.assertEqual(voice_core.parse_command("agent"), {"cmd": "wake", "text": ""})

    def test_the_page_wires_hands_free_to_the_chat_box(self):
        js = (ROOT / "static" / "voice.js").read_text()
        self.assertIn('textarea[data-voice="chat"]', js)
        self.assertIn('"/api/voice/chat"', js)
        self.assertIn('c.cmd !== "stop" && c.cmd !== "wake"', js)   # only stop/wake are commands there
        self.assertIn('getElementById("chatstop")', js)             # "agent cancel" presses Stop
        self.assertIn("say: (t) => speak(t, false)", js)            # talk-back obeys Talk-back off


class Pages(unittest.TestCase):
    """The real handler on a free port: the page, the routes, the guard, the SSE."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="chat-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.CHATS, jobboard.PORT, jobboard.READONLY,
                     jobboard.SETTINGS_FILE, jobboard.ACCESS_FILE, jobboard.BOUND)
        cls.jobs = cls.tmp / "jobs"; cls.jobs.mkdir()
        cls.work = cls.tmp / "work"
        cls.work.mkdir()
        (cls.work / "proj").mkdir()
        (cls.work / "other.app").mkdir()
        cls.chats = cls.tmp / "chats"
        cls.settings = cls.tmp / "settings.json"
        cls.settings.write_text(json.dumps({"watchdog_tick_s": 1, "after_result_grace_s": 2}))
        jobboard.JOBS, jobboard.WORK = cls.jobs, cls.work
        jobboard.CHATS, jobboard.SETTINGS_FILE = cls.chats, cls.settings
        jobboard.ACCESS_FILE = cls.tmp / "access-settings.json"
        jobboard.READONLY = False
        cls.port = free_port()
        jobboard.PORT = cls.port                       # guard() reads this per request
        cls.host = mock.patch.object(upgrades, "HOST_MODE", True); cls.host.start()
        cls.modes = mock.patch.dict(jobboard.MODES, {upgrades.FAKE_MODE: "Fake agent (tests only)"})
        cls.modes.start()
        cls.mode = mock.patch.object(chat, "CHAT_MODE", upgrades.FAKE_MODE); cls.mode.start()
        # header() asks the model server what it thinks; these tests never want that round trip
        cls.basalt = mock.patch.object(jobboard, "basalt_status", lambda: "loaded"); cls.basalt.start()
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        for patch in (cls.basalt, cls.mode, cls.modes, cls.host):
            patch.stop()
        cls.srv.shutdown(); cls.srv.server_close(); cls.thr.join(timeout=5)
        (jobboard.JOBS, jobboard.WORK, jobboard.CHATS, jobboard.PORT, jobboard.READONLY,
         jobboard.SETTINGS_FILE, jobboard.ACCESS_FILE, jobboard.BOUND) = cls.saved
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        if self.chats.exists():
            for d in self.chats.iterdir():
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                else:
                    d.unlink(missing_ok=True)
        phoneaccess_off()
        self.cookies = ""

    def tearDown(self):
        self.no_turns_running()

    def no_turns_running(self):
        """Never leave a turn thread writing into folders this class is about to delete."""
        deadline = time.time() + 60
        while time.time() < deadline:
            live = [c for c in chat.list_chats(jobboard.CHATS) if chat.turn_active(c)]
            if not live:
                return
            for rec in live:
                chat.stop_turn(jobboard.chat_ctx(), rec["id"])
            time.sleep(0.2)
        self.fail("a chat turn would not end: " + json.dumps(
            [(c["id"], (c.get("turn") or {}).get("state")) for c in chat.list_chats(jobboard.CHATS)]))

    # ---- fetch
    def fetch(self, path, data=None, cookie=True, origin=None, phone=False):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                     method="POST" if body is not None else "GET")
        if cookie:
            req.add_header("Cookie", f"jb={jobboard.TOKEN}" + (f"; {self.cookies}" if self.cookies else ""))
        elif self.cookies:
            req.add_header("Cookie", self.cookies)
        if origin:
            req.add_header("Origin", origin)
        seam = mock.patch.object(jobboard.H, "remote", lambda s: PHONE if phone else "127.0.0.1")
        with seam:
            try:
                with OPENER.open(req, timeout=30) as r:
                    return r.status, r.read().decode("utf-8", "replace"), r.headers
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace"), e.headers

    def phone(self, path, data=None, origin=True, cookie=False):
        """The same fetch, but the peer address is a phone, not this PC (as test_u09a_phone does)."""
        return self.fetch(path, data, cookie=cookie, phone=True,
                          origin=(origin if origin is not True else f"http://{PHONE}:{self.port}"))

    def login(self):
        code, _b, hdrs = self.phone("/login", {"password": PW})
        self.assertEqual(code, 303)
        c = [v for v in (hdrs.get_all("Set-Cookie") or []) if v.startswith("ajauth=")]
        self.assertTrue(c, "the login page should set a session cookie")
        self.cookies = c[0].split(";")[0]

    def jpost(self, path, obj, origin=None, cookie=True):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Origin": origin or f"http://127.0.0.1:{self.port}",
                                              "Cookie": (f"jb={jobboard.TOKEN}" if cookie else "")})
        try:
            with OPENER.open(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.headers

    # ---- the page
    def test_the_chat_link_is_in_the_menu(self):
        for path in ("/", "/chat", "/projects", "/bench"):
            code, page, _ = self.fetch(path)
            self.assertEqual(code, 200, path)
            self.assertIn('<a href="/chat">Chat</a>', page, path)

    def test_the_chat_page_offers_a_folder_and_a_message_box(self):
        code, page, _ = self.fetch("/chat")
        self.assertEqual(code, 200)
        self.assertIn('action="/chat/new"', page)
        self.assertIn('data-voice="chat"', page)        # hold-to-talk fills it
        self.assertIn("<select name=\"folder\">", page)
        self.assertIn('<option value="proj">', page)
        self.assertIn('<option value="other.app">', page)
        self.assertNotIn('<option value="eval"', page)  # eval is never a target, not even here
        self.assertIn("<h2>Chats</h2>", page)
        self.assertIn("No chats yet.", page)
        self.assertIn("Start chat", page)
        self.assertIn("/static/voice.js", page)

    def test_the_new_chat_form_creates_the_chat_and_answers(self):
        code, _b, hdrs = self.fetch("/chat/new", {"folder": "proj", "text": scen({"say": "hello jason"})})
        self.assertEqual(code, 303)
        loc = hdrs["Location"]
        self.assertTrue(loc.startswith("/chat/"))
        cid = loc.split("/")[-1]
        self.assertTrue(chat.CHAT_ID_RE.fullmatch(cid))
        code2, page, _ = self.fetch(loc)
        self.assertEqual(code2, 200)
        rec = self.wait_turn(cid, 2)
        self.assertEqual(rec["folder"], "proj")
        self.assertTrue(rec["session"], "the first turn left a session to continue with")
        self.assertEqual([m["role"] for m in rec["messages"]], ["you", "agent"])
        self.assertEqual(rec["messages"][1]["text"], "hello jason")
        # the answer is on the page, in a bubble
        code2, page, _ = self.fetch(loc)
        self.assertIn("hello jason", page)
        self.assertIn('class="bub agent"', page)
        # it is listed, and opening it shows the conversation
        code3, page3, _ = self.fetch(f"/chat/{cid}")
        self.assertIn(cid, page3)
        self.assertIn('<a href="/chat/', page3)

    def test_a_chat_needs_a_folder_and_a_message(self):
        for form in ({"folder": "bad folder", "text": "hi"}, {"folder": "eval", "text": "hi"},
                     {"folder": "proj", "text": "   "}):
            code, page, _ = self.fetch("/chat/new", form)
            self.assertEqual(code, 400, form)
            self.assertIn("Pick a project folder", page)
        self.assertEqual(chat.list_chats(jobboard.CHATS), [])

    def test_a_chat_id_in_the_url_is_checked_before_it_becomes_a_path(self):
        for path in ("/chat/nope", "/chat/..%2F..%2Fetc%2Fpasswd", "/chat/20261112-100000-zzz",
                     "/chat/20261112-100000-abcd/secret", "/api/chat/nope/events",
                     "/api/chat/nope/speech"):
            code, _b, _h = self.fetch(path)
            self.assertEqual(code, 404, path)

    def test_the_page_escapes_everything_it_got_from_the_chat(self):
        rec = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)
        evil = '<script>alert("x")</script>'
        rec["title"] = 'a<b>&c "d"'
        rec["messages"] = [{"role": "you", "text": evil, "time": time.time()},
                           {"role": "agent", "text": "safe & sound <b>", "time": time.time()}]
        chat.save(jobboard.CHATS, rec)
        code, page, _ = self.fetch(f"/chat/{rec['id']}")
        self.assertEqual(code, 200)
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;alert", page)
        self.assertIn("safe &amp; sound &lt;b&gt;", page)
        self.assertNotIn('title="a<b>&c', page)
        code2, rows, _ = self.fetch("/chat")
        self.assertIn("a&lt;b&gt;&amp;c", rows)

    def test_send_appends_to_the_same_conversation_and_refuses_a_second_turn(self):
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        first = scen({"say": "one"}, {"sleep": 30})
        self.fetch(f"/chat/{cid}/send", {"text": first})
        code, _b, hdrs = self.fetch(f"/chat/{cid}/send", {"text": "and then?"})
        self.assertEqual(code, 303)
        self.assertIn("note=", hdrs["Location"])            # the page is told why nothing happened
        rec = chat.load(jobboard.CHATS, cid)
        self.assertEqual([m["text"] for m in rec["messages"]], [first], "the refused phrase was not added")
        chat.stop_turn(jobboard.chat_ctx(), cid)
        self.no_turns_running()
        self.fetch(f"/chat/{cid}/send", {"text": scen({"say": "two"})})
        rec2 = self.wait_turn(cid, 3)
        # the stopped turn never answered, so the conversation is your two messages and this reply
        self.assertEqual([m["role"] for m in rec2["messages"]], ["you", "you", "agent"])
        self.assertEqual(rec2["messages"][-1]["text"], "two")
        self.assertTrue(rec2["session"], "the turn that ran left a session to carry on with")

    def wait_turn(self, cid, replies, timeout=60):
        deadline = time.time() + timeout
        rec = None
        while time.time() < deadline:
            rec = chat.load(jobboard.CHATS, cid)
            if rec and len(rec["messages"]) >= replies and not chat.turn_active(rec):
                return rec
            time.sleep(0.2)
        self.fail(f"chat {cid} did not get {replies} replies: "
                  f"{json.dumps([(m['role'], m['text'][:40]) for m in (rec or {}).get('messages', [])])} "
                  f"turn={json.dumps((rec or {}).get('turn'))}")

    def test_stop_from_the_page_ends_the_turn(self):
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        self.fetch(f"/chat/{cid}/send", {"text": scen({"sleep": 40})})
        code, _b, hdrs = self.fetch(f"/chat/{cid}/stop", {"x": "1"})
        self.assertEqual(code, 303)
        deadline = time.time() + 30
        rec = chat.load(jobboard.CHATS, cid)
        while time.time() < deadline:
            rec = chat.load(jobboard.CHATS, cid)
            if rec["turn"]["state"] not in chat.ACTIVE:
                break
            time.sleep(0.2)
        self.assertEqual(rec["turn"]["state"], "stopped",
                         f'running={list(jobboard.running)} monitors={list(jobboard.monitors)} '
                         f'turn={json.dumps(rec["turn"])}')
        code2, page, _ = self.fetch(f"/chat/{cid}")
        self.assertIn("Stopped by you", page)
        self.assertNotIn('id="chatstop"', page)      # nothing left to stop

    def test_the_stop_button_and_the_stream_are_only_there_while_a_turn_runs(self):
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        _code, page, _ = self.fetch(f"/chat/{cid}")
        self.assertNotIn('id="chatstop"', page)
        self.fetch(f"/chat/{cid}/send", {"text": scen({"say": "x"}, {"sleep": 8})})
        _code2, page2, _ = self.fetch(f"/chat/{cid}")
        self.assertIn('id="chatstop"', page2)
        self.assertIn('action="/chat/' + cid + '/stop"', page2)
        self.assertIn('new EventSource("/api/chat/"+cid+"/events?log="+off)', page2)
        self.assertIn('data-voice="chat"', page2)
        self.assertIn('<button id="chatsend">Send</button>', page2)
        chat.stop_turn(jobboard.chat_ctx(), cid)
        self.no_turns_running()

    def test_the_live_stream_carries_the_rows_and_the_status_then_ends(self):
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        self.fetch(f"/chat/{cid}/send", {"text": scen({"cmd": "pytest -q"}, {"say": "all green"})})
        self.wait_turn(cid, 2)
        code, body, hdrs = self.fetch(f"/api/chat/{cid}/events")
        self.assertEqual(code, 200)
        self.assertTrue(hdrs["Content-Type"].startswith("text/event-stream"))
        evs = parse_sse([body])
        kinds = [e for e, _ in evs]
        self.assertIn("status", kinds)
        self.assertIn("log", kinds)
        self.assertEqual(kinds[-1], "end")
        logs = [d for e, d in evs if e == "log"]
        items = [i for d in logs for i in d["items"]]
        self.assertTrue([i for i in items if i["k"] == "tool" and i["t"] == "pytest -q"])
        self.assertTrue([i for i in items if i["k"] == "text" and "all green" in i["t"]])
        status = [d for e, d in evs if e == "status"][-1]
        self.assertEqual(status["state"], "done")
        self.assertFalse(status["can_stop"])
        # the byte offset a reload would resume from is the whole log
        self.assertGreater(logs[-1]["next"], 0)
        self.assertLessEqual(logs[-1]["next"], status["log_size"])
        # a resume asks for what it already showed: nothing comes back but the status and the end
        code2, body2, _ = self.fetch(f"/api/chat/{cid}/events?log={logs[-1]['next']}")
        evs2 = parse_sse([body2])
        self.assertEqual([e for e, _ in evs2 if e == "log"], [])
        self.assertEqual(evs2[-1][0], "end")

    def test_the_speech_route_reads_only_the_agents_own_words(self):
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        text = "Nice one.\n\n```bash\nrm -rf nothing\n```\n\n" + ("Many words here. " * 120)
        self.fetch(f"/chat/{cid}/send", {"text": scen({"cmd": "pytest -q"}, {"say": text})})
        self.wait_turn(cid, 2)
        code, raw, _ = self.fetch(f"/api/chat/{cid}/speech")
        self.assertEqual(code, 200)
        said = json.loads(raw)["text"]
        self.assertIn("Nice one", said)
        self.assertNotIn("rm -rf", said, "code is never read aloud")
        self.assertNotIn("pytest -q", said, "tool calls are never read aloud")
        self.assertTrue(said.endswith(chat.SPEECH_TAIL))
        self.assertLess(len(said), 800)

    def test_hands_free_post_sends_a_phrase_to_the_chat_parser(self):
        code, raw, _ = self.jpost("/api/voice/chat", {"text": "agent what is the newest change?"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw), {"command": {"cmd": "say", "text": "what is the newest change?"}})
        self.assertEqual(json.loads(self.jpost("/api/voice/chat", {"text": "agent"})[1]),
                         {"command": {"cmd": "wake", "text": ""}})
        self.assertEqual(json.loads(self.jpost("/api/voice/chat", {"text": "agent cancel"})[1]),
                         {"command": {"cmd": "cancel", "text": ""}})
        self.assertEqual(json.loads(self.jpost("/api/voice/chat", {"text": "hello there"})[1]),
                         {"command": None})
        # the fixed board commands still answer for the other pages
        self.assertEqual(json.loads(self.jpost("/api/voice/command", {"text": "agent status"})[1]),
                         {"command": {"cmd": "status", "text": ""}})

    # ---- the guard
    def test_a_logged_out_phone_gets_the_login_page_and_no_chat(self):
        phoneaccess_on()
        code, _b, hdrs = self.fetch("/chat", cookie=False, phone=True)
        self.assertEqual(code, 303)
        self.assertTrue(hdrs["Location"].startswith("/login?next="))
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        code2, page, _ = self.fetch(f"/chat/{cid}", cookie=False, phone=True)
        self.assertEqual(code2, 303)
        for path, form in ((f"/chat/{cid}/send", {"text": "hello"}), (f"/chat/{cid}/stop", {"x": "1"}),
                           ("/chat/new", {"folder": "proj", "text": "hi"}),
                           ("/api/voice/chat", None)):
            code3, _b3, _h3 = self.fetch(path, form or {"j": "1"}, cookie=False, phone=True)
            self.assertEqual(code3, 403, path)
        self.assertEqual(chat.load(jobboard.CHATS, cid)["messages"], [])   # nothing happened
        self.assertEqual([c["id"] for c in chat.list_chats(jobboard.CHATS)], [cid])
        code4, _b4, _h4 = self.fetch(f"/api/chat/{cid}/events", cookie=False, phone=True)
        self.assertEqual(code4, 303)

    def test_a_post_from_another_origin_is_refused(self):
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        for path, form in ((f"/chat/{cid}/send", {"text": "hello"}), (f"/chat/{cid}/stop", {"x": "1"}),
                           ("/chat/new", {"folder": "proj", "text": "hi"})):
            code, _b, _h = self.fetch(path, form, origin="http://evil.example")
            self.assertEqual(code, 403, path)
        code, raw, _ = self.jpost("/api/voice/chat", {"text": "agent hi"}, origin="http://evil.example")
        self.assertEqual(code, 403)
        self.assertEqual(chat.load(jobboard.CHATS, cid)["messages"], [])
        code2, _b2, _h2 = self.fetch(f"/api/chat/{cid}/events", origin="http://evil.example")
        self.assertEqual(code2, 200)     # a GET is not a POST: the Origin rule is for POSTs

    def test_no_token_no_chat(self):
        """Every page needs the board's login; a chat POST is no different."""
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        for path in ("/chat", f"/chat/{cid}", f"/api/chat/{cid}/events", f"/api/chat/{cid}/speech"):
            code, _b, _h = self.fetch(path, cookie=False)
            self.assertEqual(code, 403, path)
        for path, form in (("/chat/new", {"folder": "proj", "text": "hi"}),
                           (f"/chat/{cid}/send", {"text": "hi"}), (f"/chat/{cid}/stop", {"x": "1"}),
                           ("/api/voice/chat", {"text": "agent hi"})):
            code, _b, _h = self.fetch(path, form, cookie=False)
            self.assertEqual(code, 403, path)
        self.assertEqual(chat.load(jobboard.CHATS, cid)["messages"], [])

    def test_a_read_only_board_never_starts_a_turn(self):
        jobboard.READONLY = True
        try:
            cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
            for path, form in ((f"/chat/{cid}/send", {"text": "hi"}), ("/chat/new", {"folder": "proj", "text": "hi"}),
                               (f"/chat/{cid}/stop", {"x": "1"})):
                code, page, _ = self.fetch(path, form)
                self.assertEqual(code, 403, path)
                self.assertIn("read-only", page)
            self.assertEqual(chat.load(jobboard.CHATS, cid)["messages"], [])
            code2, page2, _ = self.fetch("/chat")        # reading still works
            self.assertEqual(code2, 200)
        finally:
            jobboard.READONLY = False

    # ---- the page on a phone
    def test_the_chat_page_script_parses_with_node(self):
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        code, page, _ = self.fetch(f"/chat/{cid}")
        self.assertEqual(code, 200)
        if not shutil.which("node"):
            self.skipTest("node is not installed: cannot syntax-check the page scripts")
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        self.assertTrue(scripts, "the chat page has a script")
        for n, src in enumerate(scripts):
            p = self.tmp / f"page-{n}.js"
            p.write_text(src, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"chat page script #{n} failed node --check:\n{r.stderr}")

    def test_the_chat_page_has_its_own_phone_block(self):
        _code, page, _ = self.fetch("/chat")
        self.assertIn(f"@media (max-width:{phonelayout.PHONE_MAX}px)", page)
        for n, name in enumerate(chat.CHAT_COLS, 1):
            self.assertIn(f'table.chats td:nth-child({n})::before{{content:"{name}"}}', page)
        self.assertIn("#chattext{min-height:110px}", page)      # a box a thumb can use
        self.assertIn("min-height:%dpx" % phonelayout.TAP, page)

    def test_a_phone_signed_in_can_open_a_chat_and_send_to_it(self):
        phoneaccess_on()
        code, _b, _h = self.phone("/chat")
        self.assertEqual(code, 303)                      # not signed in yet
        self.login()
        code2, page, _ = self.phone("/chat")
        self.assertEqual(code2, 200)
        self.assertIn('data-voice="chat"', page)
        cid = chat.new_chat(jobboard.CHATS, "proj", upgrades.FAKE_MODE)["id"]
        code3, _b3, hdrs = self.phone(f"/chat/{cid}/send", {"text": scen({"say": "hi from the phone"})})
        self.assertEqual(code3, 303)
        self.assertEqual(hdrs["Location"], f"/chat/{cid}")
        rec = self.wait_turn(cid, 2)
        self.assertIn("hi from the phone", rec["messages"][-1]["text"])
        # and it can stop one
        self.phone(f"/chat/{cid}/send", {"text": scen({"sleep": 30})})
        code4, _b4, _h4 = self.phone(f"/chat/{cid}/stop", {"x": "1"})
        self.assertEqual(code4, 303)
        deadline = time.time() + 30
        while time.time() < deadline and chat.turn_active(chat.load(jobboard.CHATS, cid)):
            time.sleep(0.2)
        self.assertEqual(chat.load(jobboard.CHATS, cid)["turn"]["state"], "stopped")


def phoneaccess_on():
    """Phone access on with a password, the way the Access page would leave it (as test_u09a does)."""
    from agentjobs_upgrades import auth
    phoneaccess = jobboard.phoneaccess
    phoneaccess.save(jobboard.ACCESS_FILE, phoneaccess.Access(
        phone_access=True, listen=PHONE, password=auth.hash_password(PW, iterations=FAST)))
    jobboard.BOUND = PHONE


def phoneaccess_off():
    jobboard.phoneaccess.save(jobboard.ACCESS_FILE, jobboard.phoneaccess.Access())
    jobboard.BOUND = "127.0.0.1"


if __name__ == "__main__":
    unittest.main()
