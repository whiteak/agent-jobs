#!/usr/bin/env python3
"""fakeserver: a tiny fake OpenAI-compatible streaming server for the bench tests.

It is a stand-in for Basalt so the bench tests can run WITHOUT ever touching the
real server (we are running on the very model the bench measures). It speaks
just enough of the API for llm_bench.chat():

  GET  /v1/models              one model with id, meta.n_ctx and status "loaded"
  POST /v1/chat/completions    SSE stream: "data:" content chunks, a final chunk
                               with finish_reason + usage, then data: [DONE]

The answers come from the real task prompts in bench-ref so the existing grading
runs FOR REAL in the tests: for every task the server knows the reference answer
(reference.py wrapped in a ```python block, reference_answer.md for the puzzle,
the needle facts for longctx) which grades full marks, and a clearly wrong
answer which grades zero. Which one it sends depends on the "quality" setting,
checked per request - set s.quality = "bad", or point quality_file at a file
whose content is "good"/"bad" so a separate board process can be flipped
between queued runs.

Chat requests can also be made to FAIL (answer 503), which is how the tests
exercise the retry/pause path without ever touching a real server:
  fail_first=2       the first 2 chat requests (after the warm-up) answer 503,
                     the rest work - the sample is retried and scored once
  fail_file=path     a file whose content is "all" keeps failing every chat
                     request ("good"/"off" switches it back off), so a board
                     process in another process can be flipped mid-run
  s.fail_all = True  same thing in-process
GET /v1/models keeps answering: a chat endpoint that stops while /models still
answers is exactly what benchrun's retry-and-pause logic has to handle.

Stand-alone (manual smoke test):  python3 tests/fakeserver.py  -> prints the port.
"""
import http.server
import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_BENCH_DIR = os.environ.get("BENCH_DIR") or os.path.join(ROOT, "bench-ref")

MODEL_ID = "fake-model"
N_CTX = 262144
WARMUP = "Reply with OK."


def _txt(path):
    """Whole file, or "" when it cannot be read (control files, task answers)."""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _load(bench_dir):
    """prompt text -> (good_answer, bad_answer) for every task benchrun can ask for."""
    import glob
    import sys
    if bench_dir not in sys.path:
        sys.path.insert(0, bench_dir)
        sys.path.insert(0, os.path.join(bench_dir, "hard"))
    import llm_bench as lb
    import hard_ab

    def py(text):
        return f"Here is my solution.\n\n```python\n{text}\n```\n"

    answers = {WARMUP: ("OK.", "OK.")}
    for name, d in hard_ab.tasks():
        prompt = _txt(os.path.join(d, "prompt.md"))
        if os.path.exists(os.path.join(d, "grade.py")):  # puzzle: plain text, not code
            good = _txt(os.path.join(d, "reference_answer.md"))
            bad = "Q1=1\nQ2=2\nQ3=monday\nQ4=4\nQ5=5\n"
        else:
            good = py(_txt(os.path.join(d, "reference.py")))
            bad = py("raise SystemExit('wrong')\n")
        answers[prompt] = (good, bad)
    for name, prompt, test, extra, ref in list(lb.code_tasks()) + list(lb.bugfix_tasks()):
        answers[prompt] = (py(_txt(ref)),
                           py("raise SystemExit('wrong')\n"))
    # longctx: grade_longctx wants the "close" fact (04:15, the one that overrides
    # the earlier value) and the alias; the bad answer parrots the superseded 02:40
    good = ("close = 04:15 (the 60-minute provisional value is superseded by the "
            "final 95-minute window)\nalias = \"kestrel-7\"\n")
    bad = "close = 02:40\nalias = \"wren-2\"\n"
    for chars in lb.LONGCTX_SIZES.values():
        for rep in range(12):
            answers[lb.longctx_prompt(chars, rep)] = (good, bad)
    return answers


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "FakeBench/1"

    def log_message(self, *a):  # quiet
        pass

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"object": "list", "data": [{
                "id": MODEL_ID, "object": "model", "owned_by": "fake",
                "meta": {"n_ctx": N_CTX, "vocab_type": 2, "n_train_tokens": 1},
                "status": {"value": "loaded", "prompt": ""}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        raw = self._read_body()
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            self.send_error(400)
            return
        prompt = ((body.get("messages") or [{}])[-1].get("content") or "")
        if prompt != WARMUP:  # the warm-up never fails: that path tests exit 2
            self.server.chats += 1
            if self.server.fail_all() or self.server.chats <= self.server.fail_first():
                self.send_error(503, "fake server refuses chat requests")
                return
        good, bad = self.server.answers.get(prompt, ("OK.", "OK."))
        text = bad if self.server.quality() == "bad" else good
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self._stream(text, bool(body.get("stream")))

    def _stream(self, text, streamed):
        # a handful of chunks with tiny gaps so ttft/decode_tps come out non-null
        step = max(1, len(text) // 6)
        parts = [text[i:i + step] for i in range(0, len(text), step)] or [""]
        import time
        for p in parts:
            self.wfile.write(b"data: " + json.dumps(
                {"choices": [{"delta": {"content": p}}]}).encode() + b"\n\n")
            self.wfile.flush()
            time.sleep(0.01)
        final = {"choices": [{"delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 120, "completion_tokens": max(2, len(parts)),
                           "total_tokens": 122}}
        self.wfile.write(b"data: " + json.dumps(final).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class FakeServer:
    """FakeServer(quality="good") or FakeServer(quality_file=path).
    .port is the free port it bound; .stop() shuts it down (always)."""

    def __init__(self, bench_dir=None, quality="good", quality_file=None,
                 fail_first=0, fail_file=None):
        self.answers = _load(bench_dir or DEFAULT_BENCH_DIR)
        self._quality = quality
        self.quality_file = quality_file
        self.fail_first = int(fail_first)   # first N chat requests answer 503
        self.fail_file = fail_file          # content "all" = keep failing
        self._fail_all = False
        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._srv.answers = self.answers
        self._srv.quality = lambda: self.quality      # read per request, so tests can flip
        self._srv.fail_all = lambda: self.fail_all
        self._srv.fail_first = lambda: self.fail_first
        self._srv.chats = 0
        self._srv.daemon_threads = True
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def fail_all(self):
        """True while chat requests must fail - set in-process (s.fail_all = True)
        or by writing "all" to fail_file (works for a board in another process)."""
        raw = _txt(self.fail_file).strip() if self.fail_file else ""
        if raw:
            return raw.lower() == "all"           # unreadable file = not failing
        return self._fail_all

    @fail_all.setter
    def fail_all(self, v):
        self._fail_all = bool(v)

    @property
    def chats(self):
        """Non-warm-up chat requests answered since the last reset."""
        return self._srv.chats

    def reset_chats(self):
        """Start the fail_first counting over (the counter lives on the server)."""
        self._srv.chats = 0

    @property
    def quality(self):
        """Read per request: srv.quality = "bad" flips it, and so does writing
        the quality_file (lets a separate board process be flipped between runs)."""
        if self.quality_file:
            try:
                return _txt(self.quality_file).strip() or "good"
            except OSError:
                return self._quality
        return self._quality

    @quality.setter
    def quality(self, v):
        self._quality = v

    @property
    def port(self):
        return self._srv.server_address[1]

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def start(self):
        self._thread.start()
        return self.url

    def stop(self):
        self._srv.shutdown()
        self._srv.server_close()


if __name__ == "__main__":
    import time
    s = FakeServer()
    print("fake server on", s.start(), flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.stop()
