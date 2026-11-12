"""The Chat page sends without reloading, so the spoken reply is allowed to play."""
import unittest
import chat
from pathlib import Path


class SendInPlace(unittest.TestCase):
    def test_the_send_form_is_sent_with_fetch(self):
        self.assertIn('getElementById("chatform")', chat.CHAT_SCRIPT)
        self.assertIn("e.preventDefault()", chat.CHAT_SCRIPT)
        self.assertIn("new URLSearchParams(new FormData(form))", chat.CHAT_SCRIPT)

    def test_a_blocked_reply_sound_is_said_out_loud_on_screen(self):
        js = (Path(chat.__file__).with_name("static") / "voice.js").read_text()
        self.assertIn("NotAllowedError", js)


if __name__ == "__main__":
    unittest.main()
