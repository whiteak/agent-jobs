import unittest
import phoneaccess


class ListenList(unittest.TestCase):
    def test_off_is_local_only(self):
        self.assertEqual(phoneaccess.listen_list("127.0.0.1"), ["127.0.0.1"])

    def test_a_lan_address_keeps_127_0_0_1(self):
        self.assertEqual(phoneaccess.listen_list("192.168.1.13"), ["192.168.1.13", "127.0.0.1"])

    def test_every_network_already_covers_local(self):
        self.assertEqual(phoneaccess.listen_list("0.0.0.0"), ["0.0.0.0"])


if __name__ == "__main__":
    unittest.main()
