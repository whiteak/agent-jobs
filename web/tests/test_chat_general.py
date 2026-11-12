"""Talking should just work: with no project picked, a chat goes to the general chat folder."""
import unittest
import chat


class General(unittest.TestCase):
    def test_the_form_starts_on_general_chat(self):
        body = chat.new_chat_body({"folders": lambda: ["proj", "chat"]})
        self.assertIn('<option value="chat" selected>General chat (no project)</option>', body)
        self.assertEqual(body.count('value="chat"'), 1)
        self.assertNotIn("Pick a folder...", body)

    def test_a_picked_project_stays_picked(self):
        body = chat.new_chat_body({"folders": lambda: ["proj"]}, form={"folder": ["proj"]})
        self.assertIn('<option value="proj" selected>', body)
        self.assertNotIn('value="chat" selected', body)

    def test_the_board_falls_back_to_the_general_folder(self):
        import jobboard
        src = open(jobboard.__file__).read()
        self.assertIn('get("folder") or chat.GENERAL_FOLDER', src)


if __name__ == "__main__":
    unittest.main()
