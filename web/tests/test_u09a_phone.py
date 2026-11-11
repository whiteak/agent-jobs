"""Upgrade 9a: phone-friendly layout and safe phone access (web/phoneaccess.py,
web/phonelayout.py and the jobboard.py hooks).

The login engine (PBKDF2, lock-out, sessions, the cookie) is agentjobs_upgrades/auth.py and is
tested in test_rest.py; these tests are about the glue and the pages:

  * phone access is OFF by default, so the board keeps listening on 127.0.0.1 only, and the
    {{PHONE_ACCESS}} placeholder counts as "not filled in yet"
  * only a salted PBKDF2 hash is ever stored, and turning access on needs an address + a password
  * a request that does not come from 127.0.0.1 has to log in: the login page, the 30-day
    HttpOnly SameSite=Strict session cookie, 5 wrong tries -> 5 minutes locked out
  * a logged-out phone gets nothing - no page, no job text, and its POSTs are refused before
    the job record is read
  * approve/merge/reject and the two settings pages still need the upgrade 8 secret token,
    so a phone login alone cannot change anything
  * the manifest and the icons Chrome wants, and the phone CSS: one column at 700 px, 44 px
    tap targets, a sticky bar with the current status, the jobs list as cards, the timeline's
    newest 10 with "Show all", the diff in unified mode, and no fixed width over 360 px

This sandbox has only the loopback interface, so a "phone" is stood in for by patching the one
method that reads the peer address (H.remote). Nothing here contacts the model server, a real
agent or port 8090.
"""
import http.server
import json
import re
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

import dashboard
import jobboard
import phoneaccess
import phonelayout
import visioncheck            # only to read back the PNG size of the home-screen icons
from agentjobs_upgrades import auth, watchdog

HERE = Path(__file__).resolve().parent
WEB = HERE.parent
sys.path.insert(0, str(WEB))

PHONE = "100.12.34.56"          # a Tailscale-style address: a phone, not this PC
LAN = "192.168.1.20"
PW = "correct horse battery"
JID = "20261112-120000-ab12"
FAST = 1000                     # PBKDF2 rounds for the tests that only need the flow


# ------------------------------------------------------------------ helpers

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def meta(jid, folder="phproj", status="done", job="Add the dark mode switch"):
    t = time.time()
    return {"id": jid, "mode": "local-qwen", "folder": folder, "job": job, "offline": False,
            "status": status, "created": t - 600, "started": t - 600,
            "finished": t - 60 if status not in ("queued", "running", "stopping") else None,
            "exit": 0, "resume": None, "parent": None, "session": "sess-abc12345",
            "git_branch_start": "main"}


def style_blocks(page):
    """Every @media (max-width:Npx) rule in a page, as (px, body). Braces are matched rather
    than regexed because the block holds whole rules."""
    out = []
    for m in re.finditer(r"@media\s*\(max-width:(\d+)px\)", page):
        i = page.index("{", m.end())
        depth = 0
        for j in range(i, len(page)):
            if page[j] == "{":
                depth += 1
            elif page[j] == "}":
                depth -= 1
                if depth == 0:
                    out.append((int(m.group(1)), page[i + 1:j]))
                    break
    return out


def widths(css):
    """Every (property, value) in css that sets a width."""
    return re.findall(r"(min-width|max-width|width)\s*:\s*([^;}]+)", css)


def px_ok(value):
    """A width a phone can live with: no fixed px over 360 (the narrowest screen here)."""
    v = value.strip()
    if v in ("none", "auto") or "%" in v or "vw" in v or "vh" in v or "em" in v:
        return True
    m = re.search(r"(\d+(?:\.\d+)?)px", v)
    return (int(float(m.group(1))) <= phonelayout.MIN_VIEW) if m else True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


# ------------------------------------------------------------------ the settings file

class Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u09a-acc-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "access-settings.json"

    def test_off_by_default_and_still_local(self):
        acc = phoneaccess.load(self.path)
        self.assertFalse(acc.phone_access)
        self.assertEqual(acc.listen, phoneaccess.PLACEHOLDER)
        self.assertEqual(phoneaccess.bind_host(acc), "127.0.0.1")
        self.assertEqual(phoneaccess.phone_url(acc, 8090), "")
        self.assertIn("127.0.0.1", phoneaccess.status_line(acc, 8090))
        self.assertEqual(phoneaccess.hosts(8090, acc), {"127.0.0.1:8090", "localhost:8090"})

    def test_a_missing_file_is_off_not_an_error(self):
        self.assertFalse(phoneaccess.load(self.tmp / "nope.json").phone_access)

    def test_the_placeholder_address_is_not_an_address(self):
        acc = phoneaccess.Access(phone_access=True, listen=phoneaccess.PLACEHOLDER)
        self.assertEqual(phoneaccess.listen_addr(acc), "")
        self.assertEqual(phoneaccess.bind_host(acc), "127.0.0.1")      # stays local
        self.assertIn("no address", phoneaccess.status_line(acc, 8090))

    def test_a_real_address_is_listened_on_and_shown(self):
        acc = phoneaccess.Access(phone_access=True, listen=LAN)
        self.assertEqual(phoneaccess.bind_host(acc), LAN)
        self.assertEqual(phoneaccess.phone_url(acc, 8090), f"http://{LAN}:8090")
        self.assertIn(f"{LAN}:8090", phoneaccess.hosts(8090, acc))
        self.assertIn(f"http://{LAN}:8090", phoneaccess.origins(8090, acc))
        self.assertIn(f"http://{LAN}:8090", phoneaccess.status_line(acc, 8090))

    def test_junk_in_the_address_box_never_opens_a_socket(self):
        for bad in ("999.1.1.1", "evil.example", "http://192.168.1.5", "192.168.1", "1.2.3.4:80",
                    "192.168.1.20 /32", "", "{{PHONE_ACCESS}}"):
            self.assertEqual(phoneaccess.listen_addr(phoneaccess.Access(listen=bad)), "", bad)

    def test_a_saved_address_waits_for_the_next_restart(self):
        acc = phoneaccess.Access(phone_access=True, listen=LAN)
        line = phoneaccess.status_line(acc, 8090, bound="127.0.0.1")
        self.assertIn("still listening on 127.0.0.1", line)
        self.assertIn("Restart", line)

    def test_only_a_hash_ever_reaches_the_disk(self):
        errs, _note = phoneaccess.save_form(self.path, {"password1": [PW], "password2": [PW]})
        self.assertEqual(errs, [])
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn(PW, raw)
        self.assertIn("pbkdf2_sha256$", raw)
        self.assertIn("$310000$", raw)                       # the default work factor
        self.assertTrue(auth.verify_password(PW, phoneaccess.load(self.path).password))
        self.assertFalse(auth.verify_password("guess", phoneaccess.load(self.path).password))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_the_form_refuses_an_open_door_and_saves_nothing(self):
        for form, want in (({"phone_access": ["1"], "listen": [phoneaccess.PLACEHOLDER]}, "address"),
                           ({"phone_access": ["1"], "listen": [LAN]}, "password"),
                           ({"password1": ["short"], "password2": ["short"]}, "8 characters"),
                           ({"password1": ["longenoughpw"], "password2": ["other"]}, "do not match")):
            errs, _ = phoneaccess.save_form(self.path, form)
            self.assertTrue(errs, form)
            self.assertIn(want, " ".join(errs).lower(), form)
        self.assertFalse(self.path.exists())                 # nothing saved on the way

    def test_a_new_password_signs_every_phone_out(self):
        phoneaccess.save(self.path, phoneaccess.Access(phone_access=True, listen=LAN,
                                                       password=auth.hash_password(PW, iterations=FAST)))
        sid = phoneaccess.sessions(self.path).create()
        self.assertTrue(phoneaccess.sessions(self.path).valid(sid))
        errs, note = phoneaccess.save_form(self.path, {"password1": ["a better password"],
                                                       "password2": ["a better password"]})
        self.assertEqual(errs, [])
        self.assertIn("signed out", note)
        self.assertFalse(phoneaccess.sessions(self.path).valid(sid))

    def test_an_empty_password_keeps_the_one_you_have(self):
        h = auth.hash_password(PW, iterations=FAST)
        phoneaccess.save(self.path, phoneaccess.Access(password=h))
        errs, _ = phoneaccess.save_form(self.path, {"password1": [""], "password2": [""]})
        self.assertEqual(errs, [])
        self.assertEqual(phoneaccess.load(self.path).password, h)


# ------------------------------------------------------------------ login and lock-out

class Login(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="u09a-log-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "access-settings.json"
        phoneaccess.save(self.path, phoneaccess.Access(phone_access=True, listen=PHONE,
                                                       password=auth.hash_password(PW, iterations=FAST)))
        self.saved_lock = phoneaccess.LOCKOUT
        phoneaccess.LOCKOUT = auth.Lockout()
        self.addCleanup(setattr, phoneaccess, "LOCKOUT", self.saved_lock)

    def attempt(self, pw, who=PHONE):
        return phoneaccess.try_login(self.path, who, pw)

    def test_localhost_never_logs_in(self):
        self.assertTrue(phoneaccess.from_this_pc("127.0.0.1"))
        self.assertTrue(phoneaccess.signed_in(self.path, "127.0.0.1", {}))       # no cookie at all
        self.assertFalse(phoneaccess.from_this_pc(PHONE))
        self.assertFalse(phoneaccess.signed_in(self.path, PHONE, {}))

    def test_the_right_password_gives_a_session_and_the_wrong_one_does_not(self):
        ok, _msg, sid = self.attempt(PW)
        self.assertTrue(ok and phoneaccess.sessions(self.path).valid(sid))
        ok2, msg2, _ = self.attempt("not it")
        self.assertFalse(ok2)
        self.assertIn("not right", msg2)
        self.assertTrue(phoneaccess.signed_in(self.path, PHONE, {"Cookie": f"ajauth={sid}"}))

    def test_five_wrong_tries_lock_the_address_for_five_minutes(self):
        clock = {"t": 1000.0}
        phoneaccess.LOCKOUT = auth.Lockout(now=lambda: clock["t"])   # setUp gave us a fresh one
        for i in range(5):                            # the fifth try is what locks it
            ok, msg, _ = self.attempt("guess%d" % i)
            self.assertFalse(ok, i)
        self.assertIn("Locked", msg)
        self.assertIn("Too many tries", self.attempt(PW)[1])
        self.assertFalse(self.attempt(PW)[0])             # even the right password, for now
        clock["t"] += 301                             # five minutes later
        ok, _msg, sid = self.attempt(PW)
        self.assertTrue(ok, "the lock-out has to expire on its own")
        self.assertTrue(phoneaccess.sessions(self.path).valid(sid))

    def test_another_address_is_not_locked_out_by_this_one(self):
        for _ in range(5):
            self.attempt("guess", who="10.0.0.2")
        self.assertTrue(self.attempt(PW, who=PHONE)[0])

    def test_no_password_set_says_so_instead_of_asking_for_guesses(self):
        empty = self.tmp / "other.json"
        phoneaccess.save(empty, phoneaccess.Access(phone_access=True, listen=PHONE))
        ok, msg, _ = phoneaccess.try_login(empty, PHONE, PW)
        self.assertFalse(ok)
        self.assertIn("No password is set", msg)

    def test_the_session_cookie_is_a_30_day_http_only_strict_one(self):
        c = phoneaccess.cookie_header("abc123")
        self.assertTrue(c.startswith("ajauth=abc123; "))
        self.assertIn("Max-Age=2592000", c)
        self.assertIn("HttpOnly", c)
        self.assertIn("SameSite=Strict", c)
        self.assertIn("Max-Age=0", phoneaccess.cookie_gone())

    def test_a_guessed_cookie_is_not_a_session(self):
        self.assertFalse(phoneaccess.sid_from({"Cookie": "ajauth=short"}))
        self.assertFalse(phoneaccess.signed_in(self.path, PHONE, {"Cookie": "ajauth=" + "x" * 40}))


# ------------------------------------------------------------------ the live pages

class Pages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb09a-web-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY,
                     jobboard.ACCESS_FILE, jobboard.BOUND)
        jobboard.JOBS = cls.tmp / "jobs"; jobboard.JOBS.mkdir()
        jobboard.WORK = cls.tmp / "work"; (jobboard.WORK / "phproj").mkdir(parents=True)
        jobboard.ACCESS_FILE = cls.tmp / "access-settings.json"
        jobboard.READONLY = False
        cls.port = free_port()
        jobboard.PORT = cls.port                       # guard() reads this per request
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", cls.port), jobboard.H)
        cls.thr = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thr.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close(); cls.thr.join(timeout=5)
        (jobboard.JOBS, jobboard.WORK, jobboard.PORT, jobboard.READONLY,
         jobboard.ACCESS_FILE, jobboard.BOUND) = cls.saved
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.saved_lock = phoneaccess.LOCKOUT
        phoneaccess.LOCKOUT = auth.Lockout()
        self.addCleanup(setattr, phoneaccess, "LOCKOUT", self.saved_lock)
        phoneaccess.save(jobboard.ACCESS_FILE, phoneaccess.Access())     # off, as shipped
        for d in jobboard.JOBS.iterdir():
            shutil.rmtree(d, ignore_errors=True)
        (jobboard.JOBS / JID).mkdir(parents=True)
        self.m = meta(JID)
        jobboard.save_meta(self.m, JID)
        self.cookies = ""

    # ---- fetch, from the PC or from a phone

    def fetch(self, path, data=None, cookie=True, origin=None, phone=False, raw=False):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                                     method="POST" if body is not None else "GET")
        if cookie:
            req.add_header("Cookie", f"jb={jobboard.TOKEN}" + (f"; {self.cookies}" if self.cookies else ""))
        elif self.cookies:
            req.add_header("Cookie", self.cookies)
        if origin:
            req.add_header("Origin", origin)
        seam = mock.patch.object(jobboard.H, "remote", lambda s: PHONE) if phone else \
            mock.patch.object(jobboard.H, "remote", lambda s: "127.0.0.1")
        with seam:
            try:
                with OPENER.open(req, timeout=30) as r:
                    data_out = r.read()
                    return (r.status, data_out, r.headers) if raw else \
                        (r.status, data_out.decode("utf-8", "replace"), r.headers)
            except urllib.error.HTTPError as e:
                data_out = e.read()
                return ((e.code, data_out, e.headers) if raw
                        else (e.code, data_out.decode("utf-8", "replace"), e.headers))

    def as_phone(self):
        """Turn phone access on with a password, the way the Access page would."""
        phoneaccess.save(jobboard.ACCESS_FILE, phoneaccess.Access(
            phone_access=True, listen=PHONE, password=auth.hash_password(PW, iterations=FAST)))
        jobboard.BOUND = PHONE

    def login(self):
        code, _b, hdrs = self.phone("/login", {"password": PW})
        self.assertEqual(code, 303)
        c = [v for v in (hdrs.get_all("Set-Cookie") or []) if v.startswith("ajauth=")]
        self.assertTrue(c)
        self.cookies = c[0].split(";")[0]
        return c[0]

    def settings(self):
        return json.loads(jobboard.ACCESS_FILE.read_text(encoding="utf-8"))

    # ---- the PC keeps working exactly as before

    def test_localhost_needs_no_login_and_still_needs_the_token(self):
        code, page, _h = self.fetch("/")
        self.assertEqual(code, 200)
        self.assertIn("New job", page)
        code2, _b, _h2 = self.fetch("/", cookie=False)
        self.assertEqual(code2, 403)                     # the token link is still the key

    def phone(self, path, data=None, origin=True, raw=False, cookie=False):
        """The same fetch, but the peer address is a phone, not this PC."""
        return self.fetch(path, data, cookie=cookie, raw=raw, phone=True,
                          origin=(origin if origin is not True else f"http://{PHONE}:{self.port}"))

    # ---- a phone that has not logged in

    def test_a_phone_that_has_not_logged_in_is_sent_to_the_login_page(self):
        self.as_phone()
        code, _b, hdrs = self.phone(f"/job/{JID}")
        self.assertEqual(code, 303)
        self.assertTrue(hdrs.get("Location", "").startswith("/login?next="))
        self.assertIn("/job/" + JID, urllib.parse.unquote(hdrs["Location"]))
        code2, page, _h2 = self.phone("/login")
        self.assertEqual(code2, 200)
        self.assertIn('name="password"', page)
        self.assertNotIn("Add the dark mode switch", page)     # nothing of the job leaks
        self.assertNotIn('href="/projects"', page)             # and no way in from the page

    def test_a_logged_out_phone_post_is_refused_before_the_record_is_read(self):
        self.as_phone()
        for path, form in (("/stop/" + JID, {"x": "1"}), ("/decide/" + JID, {"act": "merge"}),
                           ("/new", {"mode": "local-qwen", "folder": "phproj", "job": "x"})):
            code, _b, _h = self.phone(path, form)
            self.assertEqual(code, 403, path)
        self.assertEqual(jobboard.load_meta(JID)["status"], "done")   # nothing happened

    def test_phone_access_off_means_no_login_page_is_even_needed(self):
        """Off is the default: the login page exists, but there is no address for a phone to
        reach, and the gate still holds for any request not from this PC."""
        code, page, _h = self.phone("/login")
        self.assertEqual(code, 200)
        acc = phoneaccess.load(jobboard.ACCESS_FILE)
        self.assertFalse(acc.phone_access)
        self.assertEqual(phoneaccess.bind_host(acc, "127.0.0.1"), "127.0.0.1")
        self.assertNotIn(f"{PHONE}:{self.port}", phoneaccess.hosts(self.port, acc))
        code2, _b, _h2 = self.phone(f"/job/{JID}")
        self.assertEqual(code2, 303)

    # ---- logging in

    def test_login_then_the_job_page_works_from_the_phone(self):
        self.as_phone()
        cookie = self.login()
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Max-Age=2592000", cookie)         # 30 days
        code, page, _h = self.phone(f"/job/{JID}")
        self.assertEqual(code, 200)
        self.assertIn("Add the dark mode switch", page)

    def test_five_wrong_passwords_lock_the_phone_out(self):
        self.as_phone()
        for i in range(5):
            code, page, _h = self.phone("/login", {"password": "wrong%d" % i})
            self.assertEqual(code, 403, i)
        code, page, _h = self.phone("/login", {"password": PW})
        self.assertEqual(code, 403)
        self.assertIn("Too many tries", page)
        self.assertEqual(jobboard.load_meta(JID)["status"], "done")

    def test_the_login_page_never_locks_out_the_pc(self):
        self.as_phone()
        for _ in range(6):
            self.phone("/login", {"password": "nope"})
        code, page, _h = self.fetch("/")                 # Jason's own browser is unaffected
        self.assertEqual(code, 200)

    def test_a_wrong_password_never_leaves_a_session_behind(self):
        self.as_phone()
        _code, _b, hdrs = self.phone("/login", {"password": "nope"})
        self.assertFalse([v for v in (hdrs.get_all("Set-Cookie") or []) if v.startswith("ajauth=")])

    # ---- upgrade 8 still owns the dangerous buttons and the settings

    def test_a_phone_session_alone_cannot_merge(self):
        self.as_phone()
        self.login()
        code, _b, _h = self.fetch(f"/decide/{JID}", {"act": "merge"}, origin=f"http://{PHONE}:{self.port}")
        self.assertEqual(code, 403)
        self.assertIsNone(jobboard.load_meta(JID).get("decision_log"))

    def test_the_access_page_carries_the_upgrade_8_token_and_needs_it(self):
        code, page, hdrs = self.fetch("/access")
        self.assertEqual(code, 200)
        self.assertIn("Allow phone access", page)
        toks = re.findall(r'name="ajtok" value="([^"]+)"', page)
        self.assertTrue(toks, "the Save button must carry the form token")
        sid = [v.split("=", 1)[1].split(";")[0] for v in (hdrs.get_all("Set-Cookie") or [])
               if v.startswith("aj=")]
        self.assertTrue(sid)
        self.cookies = "aj=" + sid[0]      # a browser sends the session its page came from
        # without it nothing is saved
        code2, _b, _h2 = self.fetch("/access", {"phone_access": "1", "listen": LAN},
                                    origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code2, 403)
        self.assertFalse(self.settings()["phone_access"])
        # with it, it is
        code3, _b3, _h3 = self.fetch("/access", {"phone_access": "1", "listen": LAN,
                                                 "password1": PW, "password2": PW, "ajtok": toks[0]},
                                     origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code3, 303)
        self.assertTrue(self.settings()["phone_access"])
        self.assertEqual(self.settings()["listen"], LAN)
        self.assertTrue(self.settings()["password"], "a switch with no password is an open door")

    def test_the_access_page_shows_the_address_to_open_on_the_phone(self):
        phoneaccess.save(jobboard.ACCESS_FILE, phoneaccess.Access(phone_access=True, listen=LAN))
        jobboard.BOUND = LAN
        code, page, _h = self.fetch("/access")
        self.assertEqual(code, 200)
        self.assertIn(f"http://{LAN}:{self.port}", page)
        self.assertIn("next time the board starts", page)

    def test_the_access_form_says_no_to_an_open_door(self):
        _c, page, hdrs = self.fetch("/access")
        toks = re.findall(r'name="ajtok" value="([^"]+)"', page)
        self.cookies = "; ".join(v.split(";")[0] for v in (hdrs.get_all("Set-Cookie") or [])
                                 if v.startswith("aj="))
        code, again, _h2 = self.fetch("/access", {"phone_access": "1", "listen": "{{PHONE_ACCESS}}",
                                                  "ajtok": toks[0]}, origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code, 400)
        self.assertIn("Address to listen on", again)
        self.assertFalse(self.settings()["phone_access"])

    def test_the_phone_alerts_page_needs_the_same_secret(self):
        code, page, hdrs = self.fetch("/notify")
        self.assertEqual(code, 200)
        toks = re.findall(r'name="ajtok" value="([^"]+)"', page)
        self.assertTrue(toks)
        self.cookies = "; ".join(v.split(";")[0] for v in (hdrs.get_all("Set-Cookie") or [])
                                 if v.startswith("aj="))
        code2, _b, _h2 = self.fetch("/notify", {"server": "http://192.168.1.9:2586", "topic": ["x"]},
                                    origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code2, 403)                     # a POST without the token
        code3, _b3, _h3 = self.fetch("/notify", {"server": "", "ajtok": toks[0]},
                                     origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(code3, 303)

    # ---- add to home screen

    def test_the_manifest_and_icons_need_no_login_and_hold_nothing(self):
        code, raw, hdrs = self.fetch("/manifest.webmanifest", cookie=False)
        self.assertEqual(code, 200)
        man = json.loads(raw)
        self.assertEqual(man["display"], "standalone")
        self.assertEqual(man["start_url"], "/")
        self.assertEqual([i["sizes"] for i in man["icons"]], ["192x192", "512x512"])
        self.assertNotIn("password", raw.lower())
        for name, size in (("/icon-192.png", 192), ("/icon-512.png", 512)):
            c2, body, h2 = self.fetch(name, cookie=False, raw=True)
            self.assertEqual(c2, 200, name)
            self.assertEqual(h2.get("Content-Type"), "image/png")
            self.assertEqual(visioncheck.png_size(body), (size, size), name)

    def test_every_page_offers_the_manifest_and_the_viewport(self):
        self.as_phone()
        for path in ("/", f"/job/{JID}"):
            code, page, _h = self.fetch(path)
            self.assertEqual(code, 200, path)
            self.assertIn('<meta name="viewport" content="width=device-width,initial-scale=1">', page, path)
            self.assertIn('<link rel="manifest" href="/manifest.webmanifest">', page, path)
            self.assertIn('name="theme-color"', page, path)


# ------------------------------------------------------------------ the phone layout

class Layout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="jb09a-css-"))
        cls.saved = (jobboard.JOBS, jobboard.WORK, jobboard.READONLY)
        jobboard.JOBS = cls.tmp / "jobs"; jobboard.JOBS.mkdir()
        jobboard.WORK = cls.tmp / "work"; (jobboard.WORK / "phproj").mkdir(parents=True)
        jid = JID
        (jobboard.JOBS / jid).mkdir()
        jobboard.save_meta(meta(jid), jid)
        for n in range(14):
            dashboard.tl_add(jobboard.job_dir(jid), "step", f"step {n + 1}")
        cls.home = jobboard.home_page()
        cls.job = jobboard.job_page(jid, "tok")

    @classmethod
    def tearDownClass(cls):
        (jobboard.JOBS, jobboard.WORK, jobboard.READONLY) = cls.saved
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def blocks(self, page):
        return style_blocks(page)

    def test_all_the_phone_css_shares_one_breakpoint(self):
        for page, where in ((self.home, "home"), (self.job, "job")):
            got = self.blocks(page)
            self.assertTrue(got, where)
            self.assertEqual({px for px, _ in got}, {700}, where)

    def test_no_fixed_width_over_360_px_in_the_phone_blocks(self):
        for page, where in ((self.home, "home"), (self.job, "job")):
            for _px, body in self.blocks(page):
                for prop, val in widths(body):
                    self.assertTrue(px_ok(val), f"{where}: {prop}:{val}")

    def test_the_wide_desktop_widths_are_released_on_a_phone(self):
        body = " ".join(b for _px, b in self.blocks(self.home))
        for sel in (".jobtxt", ".clamp", ".act"):
            self.assertIn(f"{sel}{{max-width:none", body.replace(" ", ""), sel)
        self.assertIn(".wrap{max-width:none", body.replace(" ", ""))

    def test_one_column_and_44_px_tap_targets(self):
        body = " ".join(b for _px, b in self.blocks(self.home))
        self.assertIn(".row{display:block}", body)
        self.assertIn(".row>div{width:100%;min-width:0", body.replace(" ", ""))
        self.assertIn("min-height:44px", body)
        for page in (self.home, self.job):
            b = " ".join(x for _px, x in self.blocks(page))
            self.assertIn("button,a.btn,summary{min-height:44px", b.replace(" ", ""))
            self.assertIn("min-height:44px", b)      # fields and check badges too

    def test_the_top_bar_is_sticky_and_carries_the_status(self):
        for page in (self.home, self.job):
            body = " ".join(b for _px, b in self.blocks(page))
            self.assertIn("header{position:sticky", body.replace(" ", ""))
            self.assertIn('id="topst"', page)
        self.assertIn("getElementById('topst')", self.home)     # the poller fills it in
        self.assertIn("getElementById('topst')", self.job)

    def test_the_jobs_list_is_a_card_table_and_the_rows_stay_plain(self):
        self.assertIn('<table class="jobs">', self.home)
        body = " ".join(b for _px, b in self.blocks(self.home)).replace(" ", "")
        self.assertTrue("table.jobs,table.jobstbody,table.jobstr,table.jobs" in body
                        or "table.jobstr{background:var(--card)" in body, body[:200])
        # every column gets its name from CSS, in the order the thead lists them
        heads = re.findall(r"<th[^>]*>([^<]+)</th>", self.home)
        self.assertEqual(heads, list(phonelayout.JOB_COLS))
        for n, name in enumerate(phonelayout.JOB_COLS, 1):
            rule = f'table.jobs td:nth-child({n})::before{{content:"{name}"}}'
            self.assertIn(rule.replace(" ", ""), body)
        # ... and /api/rows sends the very same rows the page already has
        self.assertEqual(jobboard.job_rows().count("<td"), 7 * self.home.count('<td class="now">'))

    def test_the_timeline_shows_the_newest_10_and_offers_show_all(self):
        page = self.job
        self.assertIn('id="tlmore"', page)
        self.assertIn("Show all 14 events", page)
        body = " ".join(b for _px, b in self.blocks(page))
        self.assertIn("ul.tl li:nth-last-child(n+11){display:none}", body)
        self.assertIn("ul.tl.open li{display:flex}", body)
        self.assertIn("getElementById('tlmore')", page)          # one click, no round trip

    def test_a_short_timeline_needs_no_button(self):
        d = Path(tempfile.mkdtemp(prefix="u09a-tl-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        for n in range(3):
            dashboard.tl_add(d, "step", f"step {n}")
        self.assertNotIn("tlmore", dashboard.timeline_card(d))

    def test_the_diff_viewer_stays_unified_on_a_phone(self):
        body = " ".join(b for _px, b in self.blocks(self.job))
        self.assertIn("#chview{display:none}", body)             # no side-by-side to press
        self.assertIn("function phone(){ return window.innerWidth<=700; }", diffcard_js())
        self.assertIn("if(phone()&&sbs)", diffcard_js())         # and a resize falls back

    def test_the_side_by_side_button_is_still_there_on_a_pc(self):
        self.assertIn('id="chview"', self.job)
        hits = list(re.finditer(r"#chview\s*\{\s*display:none", self.job))
        self.assertTrue(hits, "the phone block must hide it")
        for h in hits:      # ... and only there: every hide sits inside a phone media block
            before = self.job[:h.start()]
            at = before.rfind("@media (max-width:700px){")
            self.assertNotEqual(at, -1, self.job[h.start() - 120:h.start() + 40])
            self.assertGreater(before.count("{", at), before.count("}", at))


def diffcard_js():
    import diffcard
    return diffcard.JS


# ------------------------------------------------------------------ the jail still cannot see it

class Jail(unittest.TestCase):
    def test_a_job_cannot_read_the_password_hash_or_a_session(self):
        for cmd in ("cat ~/agent-sandbox/state/access-settings.json",
                    "cat /home/jason/agent-sandbox/state/phone-sessions.json",
                    "cp $HOME/agent-sandbox/state/access-settings.json /tmp/"):
            self.assertIsNotNone(watchdog.safety_reason(cmd), cmd)

    def test_the_board_own_records_are_still_fine(self):
        for cmd in ("cat ~/agent-sandbox/state/jobs/20261112-120000-ab12/meta.json",
                    "ls ~/agent-sandbox/state", "python3 -m unittest -q"):
            self.assertIsNone(watchdog.safety_reason(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
