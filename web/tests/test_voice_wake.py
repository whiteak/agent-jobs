"""Hands-free: "agent" said on its own, then a pause, then the command."""
import unittest
from pathlib import Path
import voice_core


class WakeAlone(unittest.TestCase):
    def test_the_wake_word_alone_waits_for_the_command(self):
        for heard in ("Agent.", "agent", " Agent! ", "Hey agent,", "Okay, agent?", "Agents."):
            self.assertEqual(voice_core.parse_command(heard), {"cmd": "wake", "text": ""}, heard)

    def test_the_next_phrase_is_read_as_a_command(self):
        # the page re-adds the wake word to the phrase after "Yes?"
        self.assertEqual(voice_core.parse_command("agent " + "What's the status?"), {"cmd": "status", "text": ""})
        self.assertEqual(voice_core.parse_command("agent " + "New job, add a dark mode."),
                         {"cmd": "new_job", "text": "add a dark mode"})

    def test_whole_commands_still_work_and_unknown_is_still_unknown(self):
        self.assertEqual(voice_core.parse_command("Agent, status."), {"cmd": "status", "text": ""})
        self.assertEqual(voice_core.parse_command("Agent, make me a sandwich")["cmd"], "unknown")
        self.assertIsNone(voice_core.parse_command("hello there"))

    def test_the_page_handles_wake(self):
        js = (Path(voice_core.__file__).with_name("static") / "voice.js").read_text()
        self.assertIn('case "wake":', js)
        self.assertIn("awakeUntil", js)
        self.assertIn('"agent " + heard', js)


if __name__ == "__main__":
    unittest.main()
