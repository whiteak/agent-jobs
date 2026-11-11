#!/usr/bin/env python3
"""The voice code (VOICE, 2026-11-11) exists in THIS copy too.

The live board grew voice chat after this copy branched off it. Whichever copy is
swapped in must keep voice, so the merge is pinned here: every line the live patch
adds to jobboard.py (the import, the voice.js script tag, the data-voice page hooks,
the voice helper block, H.voice_post and the six routes), the four voice files, and -
so that a merge can never quietly delete this copy's own work - one marker line from
every block this copy has and the live board does not (JAIL, U01, U04, U05, U06,
U07, U08, U09a, U10).

The lists are fixed text in this file on purpose. No test reads .live-voice-ref/:
that folder is only the snapshot the merge was made from and is deleted afterwards.
The voice routes themselves are exercised over real HTTP in tests/test_jobvoice.py.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "jobboard.py"), encoding="utf-8").read()

# Every line containing "VOICE" in the live board's jobboard.py, word for word.
VOICE_LINES = [
    'import voice_core   # VOICE: offline speech in/out (whisper worker + piper, CPU only)',
    '# ---------------------------------------------------------------- voice (VOICE)',
    'VOICE_JS = Path(__file__).with_name("static") / "voice.js"',
    'VOICE_AUDIO_MAX = 25 * 1024 * 1024',
    'VOICE_STATE = {"QUEUED": "queued", "WORKING": "running", "STUCK": "stuck",',
    '    st = VOICE_STATE.get(s["state"], s["state"].lower())',
    '        """VOICE: raw-body routes. Runs in this request\'s own thread (ThreadingHTTPServer),',
    '        limit = VOICE_AUDIO_MAX if path == "/api/transcribe" else 64 * 1024',
    '        if u.path == "/static/voice.js":   # VOICE',
    '            try: return self.send(VOICE_JS.read_bytes(), "text/javascript; charset=utf-8")',
    '        if u.path == "/api/voice/status": return self.send(json.dumps(voice_status()), "application/json")   # VOICE',
    '        if u.path == "/api/voice/report": return self.send(json.dumps(voice_report()), "application/json")   # VOICE',
    '        if vpath in ("/api/transcribe", "/api/speak", "/api/voice/command"):   # VOICE',
]

# The rest of what the patch adds: helpers, page hooks, the route bodies.
VOICE_PARTS = [
    'def voice_latest():',
    'def voice_report_md(m, fresh=False):',
    'def voice_status():',
    'def voice_report():',
    '    def voice_post(self, path):',
    '            return self.send(json.dumps({"text": voice_core.transcribe_bytes(body)}), "application/json")',
    '                return self.send(voice_core.speak_to_file(text).read_bytes(), "audio/wav")',
    '            return self.send(json.dumps({"command": voice_core.parse_command(text)}), "application/json")',
    '        except voice_core.VoiceError as e:',
    '            return self.voice_post(vpath)',
    '<script src="/static/voice.js"></script></body>',       # the PAGE template
    'data-voice="new-job"',                                  # the new-job box
    'data-voice="follow-up" data-job="{esc(jid)}"',          # the follow-up box
    "<pre data-voice-read>",                                 # the report card
]

# One line per block that only this copy has. A merge that pastes the live
# jobboard.py over this one instead of patching it loses these and fails here.
COPY_MARKERS = [
    'import jailcfg   # JAIL: no-internet sandbox for local modes, self-test, Push button',
    'import upgrades   # U01: verify gate, git checkpoints, runaway watchdog',
    'import attempts   # U05: best-of-N for hard steps (fresh copies, ranking, Attempts card)',
    'import planning   # U04a: an outline -> a plan of milestones and tasks to check before anything runs',
    'import taskqueue  # U04b: an approved plan runs as a queue of tasks, one job at a time',
    'import visioncheck  # U06: screenshot the app and ask the model whether it looks right',
    'import androidcheck  # U06b: the same for an Android app - install it and start it on a device',
    'import dashboard  # U07: timeline, live updates (SSE), live log, badges',
    'import badges     # U07b: one badge per check, linking to its output',
    'import decisions  # U08: Approve & merge / Ask for changes / Reject, for a person at the page only',
    'import phoneaccess  # U09a: phone access (off by default), the password login, the Access page',
    'import morningreport  # U10: the morning report - what ran overnight, and what needs Jason',
    # the page template keeps this copy's phone CSS slot AND gains the voice script
    '</style>@@PHONE@@</head><body><div class="wrap">@@BODY@@</div><script src="/static/voice.js"></script></body></html>"""',
    # the job page keeps this copy's cards around the voice-marked report card
    '{follow_html}{dashboard.timeline_card(job_dir(jid))}{dashboard.log_card(jid)}{dashboard.CSS}',
    '{diffcard.card_html(jid, active)}{diffcard.CSS}{badges.CSS}',
    # do_GET keeps this copy's pre-login static routes above the voice ones
    '        if up.path in ("/manifest.webmanifest", "/manifest.json"):',
    # do_POST keeps the U09a login before the guard, the guard before voice, voice before READONLY
    '        if urllib.parse.urlparse(self.path).path == "/login":',
    '        if not self.guard(post=True): return',
    '        if READONLY:',
    # U07's SSE handler still sits where voice_post was inserted
    '    def sse_job(self, jid, q):',
]

VOICE_FILES = ("voice_core.py", "voice_worker.py", os.path.join("static", "voice.js"),
               os.path.join("tests", "test_jobvoice.py"))


class Merge(unittest.TestCase):
    def test_every_voice_line_of_the_live_board_is_here(self):
        for line in VOICE_LINES:
            self.assertIn(line, SRC.splitlines(), line)

    def test_the_voice_parts_are_here(self):
        for line in VOICE_PARTS:
            self.assertIn(line, SRC, line)

    def test_this_copy_kept_its_own_blocks(self):
        for line in COPY_MARKERS:
            self.assertIn(line, SRC, line)

    def test_voice_files_were_copied_in(self):
        for rel in VOICE_FILES:
            p = os.path.join(ROOT, rel)
            self.assertTrue(os.path.isfile(p), rel)
            self.assertGreater(os.path.getsize(p), 1000, rel)

    def test_voice_core_imports_next_to_jobboard(self):
        """jobboard.py does a plain `import voice_core`, so it has to be importable
        from this folder - and its helpers have to answer without any models."""
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        import voice_core
        self.assertEqual(voice_core.parse_command("Agent, status"), {"cmd": "status", "text": ""})
        self.assertIsNone(voice_core.parse_command("hello there"))
        self.assertEqual(voice_core.status_speech(None), "There are no jobs yet.")


if __name__ == "__main__":
    unittest.main()
