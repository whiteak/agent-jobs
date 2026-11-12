"""Phone access through `tailscale serve` (https in front of 127.0.0.1:8090)."""
import unittest
import phoneaccess
from phoneaccess import Access

NAME = "jason-b850m-c.tail1234.ts.net"


class Tailscale(unittest.TestCase):
    def acc(self, **kw):
        base = dict(phone_access=True, listen="192.168.1.13", password="x", https_name=NAME)
        base.update(kw)
        return Access(**base)

    def test_the_https_name_is_a_host_and_an_https_origin(self):
        a = self.acc()
        self.assertIn(NAME, phoneaccess.hosts(8090, a))
        self.assertIn("https://" + NAME, phoneaccess.origins(8090, a))
        self.assertNotIn("http://" + NAME, phoneaccess.origins(8090, a))
        self.assertIn("http://127.0.0.1:8090", phoneaccess.origins(8090, a))

    def test_off_or_a_bad_name_adds_nothing(self):
        for a in (self.acc(phone_access=False), self.acc(https_name="evil.com/x"),
                  self.acc(https_name="a b.ts.net"), self.acc(https_name="")):
            self.assertNotIn(a.https_name, phoneaccess.hosts(8090, a))

    def test_proxied_requests_are_not_from_this_pc(self):
        self.assertTrue(phoneaccess.proxied({"X-Forwarded-For": "100.64.0.2"}))
        self.assertTrue(phoneaccess.proxied({"Tailscale-User-Login": "j@example.com"}))
        self.assertFalse(phoneaccess.proxied({}))
        self.assertFalse(phoneaccess.from_this_pc("proxy:100.64.0.2"))

    def test_the_name_survives_save_and_load(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "access-settings.json"
            phoneaccess.save(p, self.acc())
            self.assertEqual(phoneaccess.load(p).https_name, NAME)
            phoneaccess.save(p, self.acc(https_name=""))
            self.assertEqual(phoneaccess.load(p).https_name, "")

    def test_the_board_treats_proxied_requests_as_remote(self):
        src = open(phoneaccess.__file__.replace("phoneaccess.py", "jobboard.py")).read()
        self.assertIn("phoneaccess.proxied(self.headers)", src)


if __name__ == "__main__":
    unittest.main()
