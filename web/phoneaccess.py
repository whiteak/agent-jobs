#!/usr/bin/env python3
"""Upgrade 9a: phone access - OFF by default, and a password for anything that is not this PC.

The login logic itself is the given `agentjobs_upgrades/auth.py` (PBKDF2 password hash,
`is_local`, `Lockout` = 5 tries then 300 s, `Sessions` = ids stored hashed, `cookie_header` =
HttpOnly + SameSite=Strict + 30 days, `needs_login`) - it is not reimplemented here. This
module is the glue jobboard.py calls:

    load(path) / save(path)     state/access-settings.json (0600): phone_access off, the listen
                                address - the {{PHONE_ACCESS}} placeholder until Jason chooses
                                LAN or Tailscale - and only a salted PBKDF2 hash of the password.
    bind_host(acc, default)     what main() binds. Nothing but 127.0.0.1 while phone access is
                                off, so an OFF board is unreachable from the network at all.
    hosts(port, acc)            the Host/Origin names the board answers, once it listens on a
                                network address its own pages have to be reached by that name.
    signed_in / try_login / cookie_gone
                                the gate for requests that do not come from 127.0.0.1, with the
                                lock-out and the 30-day session cookie.
    page_body / save_form / login_form
                                the Access settings page, the password form and what the phone
                                has to type in.

Approve/merge/reject and these settings still need the upgrade 8 secret (decisions.py): a login
gets you to the page, it does not make the dangerous buttons work. Standard library only.
"""
from __future__ import annotations

import dataclasses
import html
import json
import re
import time
from pathlib import Path

from agentjobs_upgrades import auth

COOKIE = "ajauth"                                 # the phone session (separate from jb= and aj=)
PLACEHOLDER = "{{PHONE_ACCESS}}"                  # "fill me in once LAN or Tailscale is chosen"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
MIN_PW = 8                                        # a shorter password on a network is not a password
SID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
SESSIONS_NAME = "phone-sessions.json"
LOCK_SECS = 300                                   # auth.Lockout's own default
MAX_TRIES = 5


@dataclasses.dataclass
class Access:
    """What the board knows about reaching it from a phone. `password` is a hash, never a word."""
    phone_access: bool = False
    listen: str = PLACEHOLDER
    password: str = ""


# ------------------------------------------------------------------ the settings file

def load(path, write_defaults=False):
    """Access from the settings file. A missing or broken file means OFF; the file is only
    created when write_defaults is set, so reading it never litters."""
    path = Path(path)
    raw = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    except (OSError, ValueError):
        raw = {}
    acc = Access(phone_access=bool(raw.get("phone_access")),
                 listen=str(raw.get("listen") or PLACEHOLDER).strip(),
                 password=str(raw.get("password") or ""))
    if write_defaults:
        save(path, acc)
    return acc


def save(path, acc):
    """Write the file 0600 - it holds the password hash and the network address."""
    path = Path(path)
    data = {"phone_access": bool(acc.phone_access), "listen": acc.listen.strip(),
            "password": acc.password}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)
    return data


# ------------------------------------------------------------------ where it listens

def listen_addr(acc):
    """The address to bind when phone access is on, or '' when it is not usable yet
    (empty, the {{PHONE_ACCESS}} placeholder, or not a plain IPv4 address)."""
    s = (acc.listen or "").strip()
    if not s or "{" in s or s == "0" or not IPV4_RE.match(s):
        return ""
    if any(int(p) > 255 for p in s.split(".")):
        return ""
    return s


def bind_host(acc, default="127.0.0.1"):
    """OFF = 127.0.0.1, always. ON with a real address = that address (0.0.0.0 = this PC's
    every network, which the page says out loud). Applies on the next restart."""
    if not acc.phone_access:
        return default
    return listen_addr(acc) or default


def phone_url(acc, port, bound=""):
    """The address Jason types into his phone, or '' when there is none to show."""
    addr = (bound or "").strip() or listen_addr(acc)
    if not acc.phone_access or "{" in addr or addr in ("", "127.0.0.1", "0.0.0.0"):
        return ""
    return f"http://{addr}:{port}"


def status_line(acc, port, bound=""):
    """One plain line for the settings page and for the board's own start-up output."""
    if not acc.phone_access:
        return "Off - the board answers 127.0.0.1 only, so a phone cannot reach it at all."
    addr = listen_addr(acc)
    if not addr:
        return ("Turned on, but there is no address to listen on yet - fill in the box below "
                "(your LAN address or your Tailscale address) and restart the board.")
    if bound and bound != addr:
        return (f"Saved for {addr}, but this board is still listening on {bound}. "
                "Restart the board to take the new address.")
    if addr == "0.0.0.0":
        return (f"Turned on: listening on every network of this PC at once - open "
                f"http://<this PC's address>:{port} on your phone.")
    url = phone_url(acc, port, addr)
    if not url:
        return "Turned on, but 127.0.0.1 is this PC itself: a phone still cannot reach it."
    return f"On - open {url} on your phone and log in with the password below."


def hosts(port, acc):
    """Host headers the board answers. The local names always; when phone access is on, its
    own address too - a phone's browser sends that one, and no other name is ever accepted."""
    out = set(LOCAL_HOSTS)
    addr = listen_addr(acc)
    if acc.phone_access and addr and addr != "0.0.0.0":
        out.add(addr)
    return {f"{h}:{port}" for h in out}


def origins(port, acc):
    """Same list for the Origin of a POST."""
    return {f"http://{h}" for h in hosts(port, acc)}


# ------------------------------------------------------------------ sessions and login

LOCKOUT = auth.Lockout(max_tries=MAX_TRIES, lock_s=LOCK_SECS)   # per address, this process


def sessions(path):
    """The session file lives next to the settings file (ids are stored hashed)."""
    return auth.Sessions(Path(path).with_name(SESSIONS_NAME))


def sid_from(headers):
    """The browser's phone-session id, or '' when there isn't a usable one."""
    mt = re.search(r"(?:^|;\s*)%s=([^;]+)" % COOKIE, (headers or {}).get("Cookie", "") or "")
    sid = mt.group(1) if mt else ""
    return sid if SID_RE.fullmatch(sid) else ""


def cookie_header(sid, days=30):
    return auth.cookie_header(COOKIE, sid, days)


def from_this_pc(remote):
    """True for a request that came in on this machine. It is the only one allowed to use the
    token link the board prints, and the only one that needs no login."""
    return auth.is_local(remote or "")


def signed_in(path, remote, headers):
    """True when this request may see the board: it came from this PC, or it holds a live
    phone session. This is the whole gate - everything else is the same as before."""
    return not auth.needs_login(remote or "", sid_from(headers), sessions(path))


def lock_left(remote, now=time.time):
    """Seconds still locked (0 = not locked)."""
    until = LOCKOUT.locked_until.get(remote or "", 0)
    return max(0, int(round(until - now())))


def try_login(path, remote, password, now=time.time):
    """The login form. -> (ok, message, new session id or '').

    With no password set there is nothing to log in with: the board says so plainly, because
    that is the one thing Jason has to do first on the PC itself."""
    acc = load(path)
    remote = remote or ""
    if not acc.password:
        return False, "No password is set yet. Open this page on the PC itself and set one.", ""
    if LOCKOUT.locked(remote):
        return False, f"Too many tries. Wait {lock_left(remote, now)} seconds and try again.", ""
    if not auth.verify_password(password or "", acc.password):
        LOCKOUT.fail(remote)
        if LOCKOUT.locked(remote):
            return False, f"Wrong password. Locked for {LOCK_SECS // 60} minutes after {MAX_TRIES} tries.", ""
        return False, "That password is not right.", ""
    LOCKOUT.success(remote)
    return True, "Logged in.", sessions(path).create()


def cookie_gone():
    """Expire the session cookie, so a signed-out phone stops offering a dead id."""
    return f"{COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"


# ------------------------------------------------------------------ the settings form

def save_form(path, form, port=0, bound=""):
    """POST /access -> (errors, note). Nothing is saved when the input is wrong, so a typo
    cannot leave a network address listening with no password. Changing the password signs
    every phone out, which is the point when someone has read it."""
    acc = load(path)
    get = lambda k: (form.get(k, [""])[0]).strip()
    on = bool(form.get("phone_access"))
    listen = get("listen") or PLACEHOLDER
    pw1, pw2 = get("password1"), get("password2")
    if form.get("logout"):                       # a button on its own: no settings change
        sessions(path).revoke_all()
        return [], "Every phone is signed out - they need the password again."
    errs = []
    if pw1 and len(pw1) < MIN_PW:
        errs.append(f"The password needs at least {MIN_PW} characters.")
    if pw1 and pw2 != pw1:
        errs.append("The two passwords do not match.")
    if on and not listen_addr(Access(listen=listen)):
        errs.append("Phone access needs an address to listen on - your LAN address "
                    "(like 192.168.1.20) or your Tailscale address (like 100.x.y.z).")
    if on and not (pw1 or acc.password):
        errs.append("Phone access needs a password - a network address without one is a "
                    "door left open.")
    if errs:
        return errs, ""
    new = Access(phone_access=on, listen=listen,
                 password=auth.hash_password(pw1) if pw1 else acc.password)
    save(path, new)
    note = "Saved. The board takes the new address when it next starts."
    if pw1:
        sessions(path).revoke_all()
        note = "Password changed - every phone is signed out. " + note
    return [], note


# ------------------------------------------------------------------ the pages

def login_form(errors=(), note="", next_path="/"):
    """The page a phone sees before it can see anything else. No nav, no job data."""
    errs = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    err_html = f'<div class="card bad"><b>Not logged in:</b><ul>{errs}</ul></div>' if errs else ""
    ok = f'<p class="ok">{html.escape(note)}</p>' if note else ""
    nxt = next_path if (next_path or "").startswith("/") and not next_path.startswith("//") else "/"
    return f"""<div class="card"><h2>Agent jobs - log in</h2>
<p class="hint">This board is on a PC at home. Type the password you set on its Access page.</p>
{err_html}{ok}
<form method="post" action="/login">
<label>Password</label><input type="password" name="password" autocomplete="current-password" required autofocus>
<input type="hidden" name="next" value="{html.escape(nxt, quote=True)}">
<p style="margin-top:14px"><button>Log in</button></p></form>
<p class="hint">Five wrong tries locks this address for {LOCK_SECS // 60} minutes. The login
lasts 30 days, so you only type it once.</p></div>"""


def page_body(path, port=0, errors=(), note="", form=None, readonly=False, bound="",
              token_field="", token=""):
    """The /access card: whether a phone may reach the board, on which address, the password,
    and what to type into the phone. `token_field`/`token` is the upgrade 8 form token -
    without it the Save button does nothing, so a login alone cannot open the door."""
    acc = load(path)

    def val(key, saved=""):
        return ((form.get(key, [""])[0]).strip() if form else "") or saved

    errs = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    err_html = f'<div class="card bad"><b>Not saved:</b><ul>{errs}</ul></div>' if errs else ""
    note_html = f'<p class="ok">{html.escape(note)}</p>' if note else ""
    ro = ('<p class="hint">This copy runs read-only, so the settings cannot be changed '
          'here.</p>') if readonly else ""
    url = phone_url(acc, port, bound)
    shown = url or ("Phone access is off." if not acc.phone_access else
                    "No address to listen on yet.")
    pw_set = bool(acc.password)
    tok = (f'<input type="hidden" name="{html.escape(token_field)}" '
           f'value="{html.escape(token, quote=True)}">' if token_field and token else "")
    btn = "" if readonly else (
        '<div class="brow" style="margin-top:12px"><button>Save</button>'
        '<button name="logout" value="1" class="ghost2">Sign out every phone</button></div>')
    return f"""<div class="card"><h2>Access from your phone</h2>
<p><b>{html.escape(status_line(acc, port, bound))}</b></p>
{note_html}{err_html}{ro}
<p class="hint">Off is the default and stays off unless you turn it on here: while it is off the
board listens on 127.0.0.1 only, so nothing on your network can reach it. Turning it on makes the
board listen on one more address, and everything that does not come from this PC has to log in
with the password below. Approve / merge / reject and the settings still need the secret
behind the job page buttons (upgrade 8), so a login alone cannot change anything dangerous.</p>
<form method="post" action="/access">
{tok}
<label style="font-weight:400;margin-top:0"><input type="checkbox" name="phone_access"
 {"checked" if (form.get("phone_access") if form else acc.phone_access) else ""}>
 Allow phone access</label>
<div class="row"><div><label>Address to listen on</label>
<input type="text" name="listen" value="{html.escape(val("listen", acc.listen))}"
 placeholder="{PLACEHOLDER}">
<span class="hint">Your LAN address (like 192.168.1.20) or your Tailscale address (like
100.x.y.z). Not 0.0.0.0 unless you want every network on this PC.
This one takes effect next time the board starts.</span></div>
<div><label>What to open on the phone</label>
<p style="margin:4px 0"><code>{html.escape(shown)}</code></p>
<span class="hint">Log in there with the password. The login lasts 30 days.</span></div></div>
<div class="row"><div><label>New password <span class="hint" style="font-weight:400">(leave
empty to keep the one you have)</span></label>
<input type="password" name="password1" autocomplete="new-password"
 placeholder="{('set - ' + ('a password is saved' if pw_set else 'none set yet'))}">
<span class="hint">Only a salted PBKDF2 hash is stored - never the password itself.
{MIN_PW} characters or more.</span></div>
<div><label>Type it again</label><input type="password" name="password2" autocomplete="new-password"></div></div>
{btn}</form></div>"""
