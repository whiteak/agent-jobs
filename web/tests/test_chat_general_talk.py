#!/usr/bin/env python3
"""Job E: "General chat (no project)" is a conversation, not a coding task (web/chat.py).

What is pinned here:

  * a turn in the General folder (chat.GENERAL_FOLDER) does NOT run the agent: `agent_argv`,
    `launch_agent` and `stop_agent_process` are never reached. It POSTs the OpenAI-shaped
    /v1/chat/completions with "stream": true and no tool list, and it starts no agent session;
  * what the model is asked is: one system message (the TALK_SYSTEM prompt + the "Board right now"
    block), then the conversation so far as user/assistant messages, then the new message;
  * the history is capped - the last 30 messages, then the oldest dropped until it fits ~20 000
    characters - and the new message is never dropped;
  * the board block holds record facts only: the running job (title, folder, how long), the last
    five finished jobs (✓/✗ and when), what still waits for Jason's OK, and today's date and time.
    Never anything a model wrote, and under ~1500 characters however many records there are;
  * the streamed pieces land in the live rows while the turn still runs (k="delta"), in the same
    turn log and the same SSE, and become one saved agent message; the turn ends "done";
  * a model that refuses, is not running, or answers with nothing ends the turn "failed" with a
    note Jason can read; Stop ends it "stopped" with the pieces that got through, and closes the
    connection - there is no agent process to kill, and none is asked for;
  * the reasoning a thinking model streams separately reaches no bubble, no saved message, no
    speech and not even the turn's raw log;
  * every other folder still runs the agent exactly as before (the fake agent answers and the
    model endpoint is never asked), and the page says which kind of chat it is.

The model server is a fake this file starts on 127.0.0.1 with a free port; the real Basalt on 8080
is never contacted, and neither is the board on 8090. The agent is the hidden fake agent
(JOBBOARD_FAKE mode + host jail), so a project chat can still be shown running through it.
"""
import http.server
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if "jobboard" not in sys.modules:
    # test_jobstatus owns the isolated-env import of jobboard (see test_jobsecurity.py); this
    # file sorts after test_chat.py, but running it on its own must work too.
    sys.path.insert(0, str(ROOT / "tests"))
    import test_jobstatus  # noqa: F401,E402
import chat       # noqa: E402
import jobboard   # noqa: E402
import upgrades   # noqa: E402

THOUGHTS = "SECRET_THOUGHTS_that_must_never_show"


def parse_sse(chunks):
    """[(event, data)] out of SSE text chunks (test_chat.py's parser)."""
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


class _Handler(http.server.BaseHTTPRequestHandler):
    """One OpenAI-shaped /v1/chat/completions, streaming what the test told it to stream."""

    server_version = "FakeBasalt/1"

    def log_message(self, *a):        # quiet
        pass

    def _event(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            self.send_error(400)
            return
        srv = self.server
        srv.requests.append(body)
        if srv.spec["status"]:
            self.send_error(srv.spec["status"], "fake model server refuses")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for bit in srv.spec["reasoning"]:        # a thinking model streams this separately
            self._event({"choices": [{"delta": {"reasoning_content": bit}}]})
        for bit in srv.spec["chunks"]:
            try:
                self._event({"choices": [{"delta": {"content": bit}}]})
            except OSError:
                srv.client_gone.set()            # the board pressed Stop and closed the socket
                return
            time.sleep(srv.spec["gap"])
        try:
            self._event({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            srv.client_gone.set()


class ModelServer:
    """ModelServer(chunks=[...], reasoning=[...], status=503, gap=0.02) on a free 127.0.0.1 port.
    .requests holds the parsed body of every chat request it answered; .spec may be changed between
    requests; .client_gone is set when the board closed the connection mid-answer."""

    def __init__(self, chunks=(), reasoning=(), status=None, gap=0.0):
        self.spec = {"chunks": list(chunks), "reasoning": list(reasoning), "status": status,
                     "gap": gap}
        self.requests = []
        self.client_gone = threading.Event()
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.requests = self.requests
        self._srv.spec = self.spec
        self._srv.client_gone = self.client_gone
        self._srv.daemon_threads = True
        self._thr = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def url(self):
        return "http://127.0.0.1:%d/v1/chat/completions" % self._srv.server_address[1]

    def start(self):
        self._thr.start()
        return self.url

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


def scen(*steps):
    """A fake-agent scenario (the fake mode's job text): one round, these steps."""
    return json.dumps({"rounds": [list(steps)]})


def chat_record(folder):
    """A chat record that was never saved, for the page tests."""
    return {"id": "20261113-100000-abcd", "folder": folder, "mode": upgrades.FAKE_MODE,
            "offline": True, "created": time.time(), "updated": time.time(), "title": "",
            "session": None, "turn": None, "messages": []}


class Talk(unittest.TestCase):
    """General-chat turns for real: the board asks a fake model server, in temp folders."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chat-talk-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.work = self.tmp / "work"
        self.proj = self.work / "proj"
        self.proj.mkdir(parents=True)
        (self.work / chat.GENERAL_FOLDER).mkdir()
        self.jobs = self.tmp / "jobs"
        self.jobs.mkdir()
        self.chats = self.tmp / "chats"
        self.settings = self.tmp / "settings.json"
        self.settings.write_text(json.dumps({"watchdog_tick_s": 1, "after_result_grace_s": 2}))
        saved = (jobboard.WORK, jobboard.JOBS, jobboard.CHATS, jobboard.SETTINGS_FILE)

        def restore():
            (jobboard.WORK, jobboard.JOBS, jobboard.CHATS, jobboard.SETTINGS_FILE) = saved
        self.addCleanup(restore)
        jobboard.WORK, jobboard.JOBS = self.work, self.jobs
        jobboard.CHATS, jobboard.SETTINGS_FILE = self.chats, self.settings
        for patch in (mock.patch.object(upgrades, "HOST_MODE", True),               # no podman
                      mock.patch.dict(jobboard.MODES, {upgrades.FAKE_MODE: "Fake agent (tests only)"}),
                      mock.patch.object(chat, "CHAT_MODE", upgrades.FAKE_MODE)):
            patch.start()
            self.addCleanup(patch.stop)

    # ---- helpers
    def ctx(self):
        return jobboard.chat_ctx()

    def serve(self, chunks=("Two ", "sentences ", "here."), reasoning=(), status=None, gap=0.0):
        """Start the fake model server and point chat.TALK_URL at it (never 8080)."""
        srv = ModelServer(chunks=chunks, reasoning=reasoning, status=status, gap=gap)
        srv.start()
        self.addCleanup(srv.stop)
        patch = mock.patch.object(chat, "TALK_URL", srv.url)
        patch.start()
        self.addCleanup(patch.stop)
        return srv

    def new(self, folder=None, mode="local-qwen"):
        """A chat record. A General chat keeps the board's real chat mode: check_record only
        demands the fake agent's JSON when the mode IS the fake agent."""
        return chat.new_chat(jobboard.CHATS, folder or chat.GENERAL_FOLDER, mode)

    def send(self, cid, text):
        tid, note = chat.start_turn(self.ctx(), cid, text)
        self.assertEqual(note, "")
        return self.wait(tid)

    def wait(self, tid, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for rec in chat.list_chats(jobboard.CHATS):
                t = rec.get("turn") or {}
                if t.get("id") == tid and t.get("state") not in chat.ACTIVE:
                    return rec
            time.sleep(0.05)
        self.fail(f"turn {tid} never finished: " + json.dumps(
            [(c["id"], (c.get("turn") or {}).get("state")) for c in chat.list_chats(jobboard.CHATS)]))

    def rows(self, cid):
        return chat.turn_items(self.ctx(), jobboard.CHATS, cid, 0)[0]

    def state_of(self, cid):
        return ((chat.load(jobboard.CHATS, cid) or {}).get("turn") or {}).get("state")

    def body(self, srv, n=0):
        """The parsed chat request the fake server answered."""
        self.assertGreater(len(srv.requests), n, "the model endpoint was never asked")
        return srv.requests[n]

    def job(self, jid, status, text, folder="proj", started=None, finished=None, **kw):
        """A job record in the board's own shape, for the "Board right now" block."""
        now = time.time()
        m = {"id": jid, "kind": "job", "mode": "local-qwen", "folder": folder, "job": text,
             "offline": True, "status": status, "created": started or now,
             "started": started or now, "finished": finished,
             "exit": 0 if status == "done" else 1, "resume": None, "parent": None}
        m.update(kw)
        d = self.jobs / jid
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(m), encoding="utf-8")
        return m

    # ---------------------------------------------------------------- the model, not the agent
    def test_a_general_turn_asks_the_model_endpoint_and_never_the_agent(self):
        srv = self.serve()

        def no_agent(*a, **kw):
            raise AssertionError("a General chat must not run the agent")

        with mock.patch.object(jobboard, "launch_agent", no_agent), \
                mock.patch.object(jobboard, "agent_argv", no_agent), \
                mock.patch.object(jobboard, "stop_agent_process", no_agent):
            rec = self.new()
            out = self.send(rec["id"], "what is running on my board?")
        self.assertEqual(len(srv.requests), 1, "one message, one model request")
        self.assertEqual(out["turn"]["state"], "done")
        self.assertEqual([m["role"] for m in out["messages"]], ["you", "agent"])
        self.assertEqual(out["messages"][-1]["text"], "Two sentences here.")
        # the agent path left nothing behind: no session, no job record, no run of any kind
        self.assertIsNone(out["session"], "a model answer has no agent session to continue")
        self.assertIsNone(jobboard.load_meta(out["turn"]["id"]), "a turn is never a job record")
        self.assertEqual([m["id"] for m in jobboard.all_jobs()], [])
        # an OpenAI chat request, streaming, and with no tools in it
        body = self.body(srv)
        self.assertIs(body["stream"], True)
        self.assertNotIn("tools", body)
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertEqual(body["messages"][-1]["content"], "what is running on my board?")

    def test_the_system_message_is_the_chat_prompt_plus_the_board_block(self):
        srv = self.serve()
        self.job("20261113-100000-aaaa", "running", "make the login page work",
                 started=time.time() - 300)
        rec = self.new()
        self.send(rec["id"], "hello")
        system = self.body(srv)["messages"][0]["content"]
        self.assertIn(chat.TALK_SYSTEM, system, "the prompt Jason edits is the one sent")
        self.assertIn("cannot run a command", system)
        self.assertIn("Board right now", system)
        self.assertIn("make the login page work", system)
        self.assertNotIn("hello", system.split("Board right now")[1], "the block is facts only")

    def test_the_second_turn_carries_the_conversation_not_an_agents_session(self):
        srv = self.serve(chunks=["First. "])
        rec = self.new()
        self.send(rec["id"], "one")
        srv.spec["chunks"] = ["Second."]
        out = self.send(rec["id"], "two")
        msgs = self.body(srv, 1)["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[1]["content"], "one")
        self.assertEqual(msgs[2]["content"], "First.", "the saved answer comes back as the assistant turn")
        self.assertEqual(msgs[3]["content"], "two")
        self.assertEqual(len(srv.requests), 2, "the history is in the messages, not a session id")
        self.assertEqual(out["messages"][-1]["text"], "Second.")

    # ---------------------------------------------------------------- the board facts
    def test_the_board_block_is_built_from_job_records_only(self):
        now = time.time()
        self.job("20261113-100000-aaaa", "running", "make the login page work", started=now - 420)
        self.job("20261113-090000-bbbb", "done", "add dark mode", finished=now - 900)
        self.job("20261113-080000-cccc", "failed", "fix the parser", finished=now - 1800)
        self.job("20261113-070000-dddd", "done", "ship the phone layout", finished=now - 2400,
                 decision_log=[{"kind": "merge", "time": now - 2300}])
        self.job("20261113-060000-eeee", "safety_stop", "curl https://evil.example/x",
                 finished=now - 3000)
        self.job("20261113-050000-ffff", "done", "write the morning report", finished=now - 3600)
        self.job("20261113-040000-gggg", "done", "tidy the docs folder", finished=now - 4200)
        block = chat.board_block(jobboard.all_jobs(), now=now)
        self.assertIn("Board right now", block)
        self.assertIn(time.strftime("%A %d %B %Y", time.localtime(now)), block)
        self.assertIn("make the login page work", block)
        self.assertIn("in proj,", block)
        self.assertIn("7 min so far", block)
        self.assertIn("\u2713 add dark mode", block)
        self.assertIn("\u2717 fix the parser", block)
        self.assertIn("\u2717 curl https://evil.example/x", block)
        self.assertEqual(block.count("\u2713") + block.count("\u2717"), 5, "the last five finished")
        self.assertNotIn("tidy the docs folder", block, "the sixth one is outside the five")
        self.assertIn("Waiting for you: 3 waiting for your OK", block,
                      "the merged job is decided; the three plain done jobs are not")
        self.assertIn("Safety stops: 1", block)

    def test_the_board_block_never_uses_what_a_model_wrote(self):
        rec = self.new()
        rec["messages"] = [{"role": "you", "text": "MODEL_WORDS_FROM_THE_CHATTER",
                            "time": time.time()},
                           {"role": "agent", "text": "MODEL_WORDS_BACK", "time": time.time()}]
        chat.save(jobboard.CHATS, rec)
        block = chat.board_block(jobboard.all_jobs(), now=time.time())
        for phrase in ("MODEL_WORDS_FROM_THE_CHATTER", "MODEL_WORDS_BACK"):
            self.assertNotIn(phrase, block)

    def test_the_board_block_is_capped_however_many_records_there_are(self):
        now = time.time()
        jobs = [{"id": f"j{n}", "status": "done", "folder": "proj",
                 "job": ("a very long job text " * 60) + str(n), "finished": now - n}
                for n in range(40)]
        jobs.append({"id": "live", "status": "running", "folder": "proj",
                     "job": "a very long running job text " * 60, "started": now - 60})
        block = chat.board_block(jobs, now=now)
        self.assertLessEqual(len(block), chat.TALK_BOARD_MAX)
        self.assertEqual(block.count("\u2713"), 5, "only the last five finished jobs")
        self.assertIn("Running: a very long running job text", block)
        self.assertLessEqual(len(chat.board_block(jobs, now=now, max_chars=120)), 120)

    def test_the_board_block_of_an_empty_board_says_so(self):
        block = chat.board_block([], now=time.time())
        self.assertIn("Running: nothing", block)
        self.assertIn("Waiting for you: nothing", block)
        self.assertNotIn("\u2713", block)

    # ---------------------------------------------------------------- the history cap
    def test_the_history_is_the_last_thirty_messages(self):
        rec = self.new()
        rec["messages"] = [{"role": ("you" if n % 2 == 0 else "agent"), "text": f"m{n}",
                            "time": time.time()} for n in range(40)]
        hist = chat.history_messages(rec)
        self.assertEqual(len(hist), chat.TALK_HISTORY_MSGS)
        self.assertEqual(hist[0]["content"], "m10", "the oldest are dropped first")
        self.assertEqual(hist[-1]["content"], "m39")
        self.assertEqual([m["role"] for m in hist][:3], ["user", "assistant", "user"])

    def test_the_history_is_cut_to_fit_the_character_cap(self):
        rec = self.new()
        rec["messages"] = [{"role": ("you" if n % 2 == 0 else "agent"),
                            "text": ("x" * 900) + f"{n:03d}", "time": time.time()}
                           for n in range(50)]
        hist = chat.history_messages(rec)
        self.assertLess(len(hist), chat.TALK_HISTORY_MSGS, "the character cap bites first")
        self.assertLessEqual(sum(len(m["content"]) for m in hist), chat.TALK_HISTORY_CHARS)
        self.assertTrue(hist[-1]["content"].endswith("049"), "the newest message is never dropped")

    def test_the_new_message_is_always_asked_even_when_the_history_is_huge(self):
        rec = self.new()
        rec["messages"] = [{"role": "you", "text": "y" * 40000, "time": time.time()}] * 5
        msgs = chat.talk_messages(rec, "the new question", [])
        self.assertEqual(msgs[-1], {"role": "user", "content": "the new question"})
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(len(msgs), 3, "system, the last history message that fits, and the new one")

    # ---------------------------------------------------------------- the bubbles and the states
    def test_the_streamed_pieces_show_in_the_chat_while_the_turn_still_runs(self):
        srv = self.serve(chunks=["Two ", "sentences ", "for you."], gap=0.25)
        rec = self.new()
        tid, _ = chat.start_turn(self.ctx(), rec["id"], "how is it going?")
        rows, live_state = [], None
        deadline = time.time() + 30
        while time.time() < deadline:
            rows = self.rows(rec["id"])
            live_state = self.state_of(rec["id"])
            if rows and live_state == "running":
                break
            time.sleep(0.02)
        self.assertTrue(rows, f"nothing streamed (state={live_state})")
        self.assertEqual(live_state, "running", "the answer arrived before the turn ended")
        self.assertEqual([r["k"] for r in rows], ["delta"] * len(rows))
        self.assertEqual(rows[0]["t"], "Two ")
        self.assertLess(len(rows), 3, "the answer was still arriving")
        out = self.wait(tid)
        self.assertEqual(chat.answer_text(self.rows(rec["id"])), "Two sentences for you.")
        self.assertEqual(out["turn"]["state"], "done")
        self.assertEqual(out["turn"]["note"], "")
        self.assertEqual(out["messages"][-1]["text"], "Two sentences for you.")
        self.assertEqual(chat.answer_text(self.rows(rec["id"])), "Two sentences for you.")
        self.assertEqual(chat.speech_text(chat.answer_text(self.rows(rec["id"]))),
                         "Two sentences for you.")
        self.assertEqual(len(srv.requests), 1)

    def test_the_turn_log_is_the_boards_own_and_never_an_agent_stream(self):
        self.serve(chunks=["Just ", "talk."])
        rec = self.new()
        out = self.send(rec["id"], "hi")
        log = chat.turn_log(jobboard.CHATS, rec["id"], out["turn"]["id"])
        self.assertTrue(log.is_file())
        lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([l["k"] for l in lines], ["delta", "delta"])
        self.assertEqual(lines[0]["type"], "talk")
        self.assertNotIn("session_id", json.dumps(lines), "no agent session was started")

    def test_the_live_stream_carries_the_answer_and_the_status_then_ends(self):
        self.serve(chunks=["Hel", "lo ", "Jason"])
        rec = self.new()
        self.send(rec["id"], "hi")
        chunks = list(chat.stream(self.ctx(), rec["id"], 0, poll_s=0.05, keepalive_s=1.0,
                                  linger_s=0.0))
        evs = parse_sse(chunks)
        items = [i for e, d in evs if e == "log" for i in d["items"]]
        self.assertEqual([i["k"] for i in items], ["delta"] * 3)
        self.assertEqual(chat.answer_text(items), "Hello Jason")
        self.assertEqual([d for e, d in evs if e == "status"][-1]["state"], "done")
        self.assertEqual(evs[-1][0], "end")

    def test_a_turn_that_has_not_answered_yet_is_waiting_for_the_model(self):
        rec = self.new()
        rec["turn"] = {"id": "20261113-100000-abcd", "text": "hi", "state": "running",
                       "started": time.time()}
        chat.save(jobboard.CHATS, rec)
        s = chat.turn_status(jobboard.CHATS, chat.load(jobboard.CHATS, rec["id"]))
        self.assertEqual((s["state"], s["label"]), ("waiting", "Waiting for the model"))
        self.assertTrue(s["can_stop"])
        log = chat.turn_log(jobboard.CHATS, rec["id"], rec["turn"]["id"])
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({"type": "talk", "k": "delta", "t": "Not"}) + "\n",
                       encoding="utf-8")
        s2 = chat.turn_status(jobboard.CHATS, chat.load(jobboard.CHATS, rec["id"]))
        self.assertEqual(s2["state"], "running")

    def test_the_turn_log_is_resumed_by_byte_offset_like_a_job_log(self):
        rec = self.new()
        p = chat.turn_log(jobboard.CHATS, rec["id"], "20261113-100000-abcd")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"type":"talk","k":"delta","t":"a"}\n{"type":"talk","k":"delta","t":"b"}\n',
                     encoding="utf-8")
        rows, nxt = chat.talk_items(p, 0)
        self.assertEqual([r["t"] for r in rows], ["a", "b"])
        self.assertEqual(chat.talk_items(p, nxt), ([], nxt))
        with p.open("a", encoding="utf-8") as fh:      # a half-written chunk is not shown yet
            fh.write('{"type":"talk","k":"del')
        self.assertEqual(chat.talk_items(p, nxt), ([], nxt))
        with p.open("a", encoding="utf-8") as fh:
            fh.write('ta","t":"c"}\n')
        self.assertEqual([r["t"] for r in chat.talk_items(p, nxt)[0]], ["c"])

    # ---------------------------------------------------------------- errors
    def test_a_model_server_that_refuses_ends_the_turn_failed_with_a_readable_note(self):
        srv = self.serve(status=503)
        rec = self.new()
        out = self.send(rec["id"], "hello?")
        self.assertEqual(out["turn"]["state"], "failed")
        self.assertIn("503", out["turn"]["note"])
        self.assertIn("model", out["turn"]["note"].lower())
        self.assertEqual(out["turn"]["how"], "model_error")
        self.assertEqual([m["role"] for m in out["messages"]], ["you"], "nothing was answered")
        self.assertEqual(len(srv.requests), 1)

    def test_a_model_server_that_is_not_running_ends_the_turn_failed(self):
        patch = mock.patch.object(chat, "TALK_URL", "http://127.0.0.1:1/v1/chat/completions")
        patch.start()
        self.addCleanup(patch.stop)
        rec = self.new()
        out = self.send(rec["id"], "hello?")
        self.assertEqual(out["turn"]["state"], "failed")
        self.assertIn("model server", out["turn"]["note"])
        self.assertEqual([m["role"] for m in out["messages"]], ["you"])

    def test_a_model_server_that_says_nothing_is_a_failed_turn_not_an_empty_answer(self):
        self.serve(chunks=[])
        rec = self.new()
        out = self.send(rec["id"], "hello?")
        self.assertEqual(out["turn"]["state"], "failed")
        self.assertIn("no answer", out["turn"]["note"])
        self.assertEqual([m["role"] for m in out["messages"]], ["you"])

    def test_a_general_chat_turn_that_fails_the_record_check_never_asks_the_model(self):
        srv = self.serve()
        rec = self.new(mode="supervillain")
        out = self.send(rec["id"], "hello")
        self.assertEqual(out["turn"]["state"], "refused")
        self.assertIn("safety check (mode)", out["turn"]["note"])
        self.assertEqual(srv.requests, [], "the model was never asked")

    def test_job_records_the_board_cannot_read_fail_the_turn_instead_of_hanging_it(self):
        srv = self.serve()
        self.job("20261113-090000-bbbb", "done", "add dark mode", finished=time.time() - 60,
                 decision_log=["merged"])      # a record left in a shape nothing expects
        rec = self.new()
        out = self.send(rec["id"], "what finished?")
        self.assertEqual(out["turn"]["state"], "failed")
        self.assertIn("model", out["turn"]["note"].lower())
        self.assertEqual(srv.requests, [], "a broken board block never reaches the model")

    def test_a_connection_that_breaks_mid_answer_says_the_server_stopped_answering(self):
        class Half:
            """Answers with one chunk, then the connection dies."""

            def __iter__(self):
                yield b'data: {"choices":[{"delta":{"content":"Part"}}]}\n\n'
                raise ConnectionResetError("the server went away")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        got = []
        with self.assertRaises(chat.TalkError) as cm:
            chat.talk_stream([{"role": "user", "content": "hi"}], got.append,
                             opener=lambda req, timeout: Half())
        self.assertIn("stopped answering in the middle", str(cm.exception))
        self.assertEqual(got, ["Part"], "what got through was still handed to the page")

    def test_a_tampered_general_chat_record_is_never_turned_into_a_model_call(self):
        srv = self.serve()
        rec = self.new()
        p = chat.chat_file(jobboard.CHATS, rec["id"])
        p.write_text(json.dumps({"id": rec["id"], "folder": "../elsewhere", "mode": "fake",
                                 "messages": []}), encoding="utf-8")
        self.assertIsNone(chat.load(jobboard.CHATS, rec["id"]))
        tid, note = chat.start_turn(self.ctx(), rec["id"], "hello")
        self.assertIsNone(tid)
        self.assertIn("gone", note)
        self.assertEqual(srv.requests, [])

    # ---------------------------------------------------------------- stop
    def test_stop_cancels_the_stream_and_keeps_what_got_through(self):
        pieces = [f"chunk{n} " for n in range(200)]
        whole = "".join(pieces)
        srv = self.serve(chunks=pieces, gap=0.02)
        rec = self.new()
        tid, _ = chat.start_turn(self.ctx(), rec["id"], "talk for ever")
        deadline = time.time() + 30
        while time.time() < deadline and not self.rows(rec["id"]):
            time.sleep(0.02)
        t0 = time.time()
        self.assertEqual(chat.stop_turn(self.ctx(), rec["id"]), tid)
        out = self.wait(tid)
        self.assertLess(time.time() - t0, 10, "the stream was cut, not waited out")
        self.assertEqual(out["turn"]["state"], "stopped")
        self.assertEqual(out["turn"]["how"], "user_stop")
        self.assertEqual(out["turn"]["note"], "Stopped by you")
        said = out["messages"][-1]["text"]
        self.assertTrue(said and whole.startswith(said), "the answer ends where Stop landed")
        self.assertLess(len(said), len(whole), "the rest of the stream never arrived")
        self.assertTrue(srv.client_gone.wait(10), "the connection to the model was closed")
        self.assertEqual(chat.answer_text(self.rows(rec["id"])), said)
        # and the chat is free again for the next message
        self.assertIsNone(chat.stop_turn(self.ctx(), rec["id"]))

    def test_stop_of_a_general_turn_never_asks_for_an_agent_process_to_be_killed(self):
        self.serve(chunks=[f"c{n} " for n in range(100)], gap=0.05)
        killed = []
        patch = mock.patch.object(jobboard, "stop_agent_process",
                                  lambda jid, p=None: killed.append(jid))
        patch.start()
        self.addCleanup(patch.stop)
        rec = self.new()
        tid, _ = chat.start_turn(self.ctx(), rec["id"], "talk for ever")
        deadline = time.time() + 30
        while time.time() < deadline and not self.rows(rec["id"]):
            time.sleep(0.02)
        chat.stop_turn(self.ctx(), rec["id"])
        out = self.wait(tid)
        self.assertEqual(out["turn"]["state"], "stopped")
        self.assertEqual(killed, [], "there is no agent process behind a General chat turn")

    def test_a_turn_stopped_before_it_starts_never_asks_the_model(self):
        srv = self.serve()
        rec = self.new()
        tid = chat.new_id()
        rec["turn"] = {"id": tid, "text": "hi", "state": "waiting", "started": time.time(),
                       "finished": None, "how": None, "note": ""}
        chat.save(jobboard.CHATS, rec)
        with chat._TURNS_LOCK:
            chat.TURNS[tid] = threading.Event()
        self.addCleanup(lambda: chat.TURNS.pop(tid, None))
        chat.TURNS[tid].set()
        out = chat.run_talk_turn(self.ctx(), rec["id"], tid)
        self.assertEqual(out["turn"]["state"], "stopped")
        self.assertEqual(out["turn"]["note"], "Stopped by you")
        self.assertEqual(srv.requests, [], "the request never went out")

    # ---------------------------------------------------------------- thinking
    def test_the_models_own_reasoning_stays_out_of_the_bubble_and_the_speech(self):
        srv = self.serve(chunks=["The answer."], reasoning=[THOUGHTS + " part one", THOUGHTS + " 2"])
        rec = self.new()
        out = self.send(rec["id"], "what is running?")
        rows = self.rows(rec["id"])
        said = chat.answer_text(rows)
        self.assertEqual(said, "The answer.")
        self.assertEqual(out["messages"][-1]["text"], "The answer.")
        self.assertEqual(chat.speech_text(said), "The answer.")
        log = chat.turn_log(jobboard.CHATS, rec["id"], out["turn"]["id"])
        for where, text in (("the raw log", log.read_text(encoding="utf-8")),
                            ("the live rows", json.dumps(rows)),
                            ("the answer", said), ("the speech", chat.speech_text(said))):
            self.assertNotIn(THOUGHTS, text, f"{where} holds the answer only")
        self.assertEqual(len(srv.requests), 1)

    # ---------------------------------------------------------------- project chats untouched
    def test_a_project_chat_still_runs_the_agent_and_never_the_model_endpoint(self):
        srv = self.serve(chunks=["THE MODEL ANSWERED - IT MUST NOT"])
        rec = self.new("proj", upgrades.FAKE_MODE)
        out = self.send(rec["id"], scen({"cmd": "pytest -q"}, {"say": "the agent answered"}))
        self.assertEqual(out["turn"]["state"], "done")
        self.assertEqual(out["messages"][-1]["text"], "the agent answered")
        self.assertTrue(out["session"], "the agent session is still carried to the next message")
        self.assertEqual(srv.requests, [], "a project chat asks the agent, not the model server")
        rows = self.rows(rec["id"])
        self.assertTrue([r for r in rows if r["k"] == "tool" and r["t"] == "pytest -q"])
        self.assertEqual([r for r in rows if r["k"] == "delta"], [])

    def test_a_general_chat_is_still_one_turn_at_a_time(self):
        self.serve(chunks=[f"c{n} " for n in range(60)], gap=0.05)
        rec = self.new()
        tid, _ = chat.start_turn(self.ctx(), rec["id"], "one")
        self.addCleanup(lambda: (chat.stop_turn(self.ctx(), rec["id"]), self.wait(tid)))
        tid2, note = chat.start_turn(self.ctx(), rec["id"], "two")
        self.assertIsNone(tid2)
        self.assertIn("still answering", note)
        self.assertEqual(len(chat.load(jobboard.CHATS, rec["id"])["messages"]), 1)


class Page(unittest.TestCase):
    """The wording of the two kinds of chat, and the script that draws a streamed answer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chat-talk-page-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.chats = self.tmp / "chats"
        saved = jobboard.CHATS
        jobboard.CHATS = self.chats
        self.addCleanup(setattr, jobboard, "CHATS", saved)

    def body(self, folder):
        return chat.open_chat_body({"folders": lambda: ["proj"], "chats": jobboard.CHATS},
                                   chat_record(folder))

    def test_the_general_chat_says_it_cannot_change_files(self):
        page = self.body(chat.GENERAL_FOLDER)
        self.assertIn("Just talk - this chat can't change files", page)
        self.assertIn("General chat (no project)", page)
        self.assertNotIn("runs the agent once in this folder", page)
        self.assertNotIn("~/agent-work/chat", page)
        self.assertIn("start a job", page)

    def test_a_project_chat_keeps_the_agent_hint(self):
        page = self.body("proj")
        self.assertIn("runs the agent once in this folder", page)
        self.assertIn("~/agent-work/proj", page)
        self.assertNotIn("Just talk", page)

    def test_the_script_draws_a_streamed_answer_into_the_open_bubble(self):
        script = max(re.findall(r"<script>(.*?)</script>", self.body(chat.GENERAL_FOLDER), re.S),
                     key=len)
        self.assertIn('it.k==="delta"', script)
        self.assertIn("(function(){", script)
        self.assertNotIn("innerHTML", script)     # everything streamed in still goes in as text

    def test_the_new_chat_form_says_what_a_general_chat_is(self):
        body = chat.new_chat_body({"folders": lambda: ["proj"]})
        self.assertIn("Just talk - this chat can't change files", body)
        self.assertIn('<option value="chat" selected>General chat (no project)</option>', body)

    def test_the_chats_list_explains_the_two_kinds(self):
        body = chat.chats_body(jobboard.CHATS)
        self.assertIn("A General chat has no project", body)
        self.assertIn("saved in ~/agent-sandbox/state/chats", body)


if __name__ == "__main__":
    unittest.main()
