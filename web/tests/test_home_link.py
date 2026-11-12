import unittest
import jobboard, phonelayout


class HomeLink(unittest.TestCase):
    def test_home_is_the_first_menu_item(self):
        h = jobboard.header()
        nav = h[h.index('<nav class="nav">'):]
        self.assertIn('<a href="/"><b>Home</b></a>', nav)
        self.assertLess(nav.index("Home"), nav.index("Projects"))

    def test_the_phone_menu_wraps_instead_of_running_off_the_screen(self):
        css = phonelayout.CSS if hasattr(phonelayout, "CSS") else open(phonelayout.__file__).read()
        self.assertIn("flex-wrap:wrap", css)


if __name__ == "__main__":
    unittest.main()
