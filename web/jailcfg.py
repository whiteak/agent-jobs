"""JAIL: no-internet sandbox support for the job board (added 2026-11-11 by Claude).

Kept in its own module so jobboard.py only needs a few small "# JAIL:" hooks:

  * LOCAL_MODES          local-claude / local-qwen: always offline, credential-free home
  * selftest()           runs ~/agent-sandbox/jail/podman-selftest.sh (cached 10 min);
                         the worker refuses to start a local job if it fails
  * record_origin()      at job START, stores the project's origin URL in the record
                         (read in the throwaway --network none container, never later)
  * push_branch()        "Push branch" button: jail/push-agent-branch.sh pushes an
                         agent/... branch from a clean mirror, outside the job's repo
  * HTML helpers         sandbox badge, push card, locked Offline box on the form

Standard library only. The agent containers never see this code or the push creds.
"""
import html, re, subprocess, threading, time
from pathlib import Path

HOME = Path.home()
JAIL = HOME / "agent-sandbox" / "jail"
SELFTEST = JAIL / "podman-selftest.sh"
PUSH = JAIL / "push-agent-branch.sh"
LOCAL_MODES = ("local-claude", "local-qwen")
SELFTEST_TTL = 600                       # seconds a passing self-test is trusted
BRANCH_RE = re.compile(r"agent/[A-Za-z0-9._/-]{1,100}")
GH_HTTPS_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")
GH_SSH_RE = re.compile(r"(?:ssh://)?git@github\.com[:/]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")

_st_lock = threading.Lock()
_st_cache = {"t": 0.0, "ok": False, "out": ""}
_push_lock = threading.Lock()


def is_local(m):
    return (m or {}).get("mode") in LOCAL_MODES


# ------------------------------------------------------------------ self-test

def selftest(force=False):
    """(ok, output). A PASS is cached for SELFTEST_TTL; a FAIL is never cached, so
    the next job re-checks (Basalt may just have been restarting)."""
    with _st_lock:
        now = time.time()
        if not force and _st_cache["ok"] and now - _st_cache["t"] < SELFTEST_TTL:
            return True, _st_cache["out"]
        try:
            r = subprocess.run([str(SELFTEST)], capture_output=True, text=True, timeout=180)
            ok, out = r.returncode == 0, (r.stdout + r.stderr).strip()
        except Exception as ex:
            ok, out = False, f"FAIL  could not run the self-test: {ex}"
        _st_cache.update(t=now, ok=ok, out=out)
        return ok, out


def selftest_failure_note(out):
    fails = [l[6:].strip() for l in out.splitlines() if l.startswith("FAIL")]
    return "Sandbox check failed: " + ("; ".join(fails[:4]) or out[-300:] or "no output")


def stop_children(jid):
    """Stop the offline local-agent containers a supervised job started through the
    broker (named agentjob-<id>-local-N). The broker also does this when it exits."""
    try:
        r = subprocess.run(["podman", "ps", "-q", "--filter", f"name=^agentjob-{jid}-local-"],
                           capture_output=True, text=True, timeout=30)
        ids = r.stdout.split()
        if ids:
            subprocess.run(["podman", "stop", "-t", "5"] + ids, capture_output=True, timeout=60)
    except Exception:
        pass


# ------------------------------------------------------------------ remotes

def normalize_remote(url):
    """A remote we are willing to push to, normalized, or None.
    GitHub (https or ssh form -> https, no user:token@ part) or a local absolute path."""
    url = (url or "").strip()
    if not url or len(url) > 300 or any(c.isspace() for c in url):
        return None
    for rx in (GH_HTTPS_RE, GH_SSH_RE):
        mt = rx.fullmatch(url)
        if mt:
            return f"https://github.com/{mt.group(1)}/{mt.group(2)}"
    if url.startswith("file://"):
        url = url[7:]
    if url.startswith("/") and re.fullmatch(r"/[A-Za-z0-9._/@+-]+", url) and ".." not in url:
        return url
    return None


def record_origin(m, in_sandbox):
    """Store the origin URL as it is right before the job starts. Called from
    record_git_start (throwaway --network none container, project mounted read-only)."""
    raw = in_sandbox(m["folder"], "git remote get-url origin 2>/dev/null", timeout=30).strip()
    m["git_origin_raw"] = raw[:300] if raw and not raw.startswith("(could not") else ""
    m["git_origin"] = normalize_remote(m["git_origin_raw"]) or ""


def agent_branches(m, in_sandbox):
    out = in_sandbox(m["folder"], "git for-each-ref --format='%(refname:short)' refs/heads/agent/ 2>/dev/null; "
                                  "echo '--current--'; git branch --show-current 2>/dev/null", timeout=30)
    names, _, cur = out.partition("--current--")
    branches = [b.strip() for b in names.splitlines() if BRANCH_RE.fullmatch(b.strip()) and ".." not in b]
    cur = cur.strip()
    return branches[:50], (cur if cur in branches else (branches[0] if branches else ""))


# ------------------------------------------------------------------ push

def push_branch(m, work_dir, branch):
    """Run push-agent-branch.sh; returns a result dict (also stored in m["push"])."""
    origin = m.get("git_origin") or ""
    res = {"time": time.time(), "branch": branch, "remote": origin, "ok": False, "output": ""}
    if not BRANCH_RE.fullmatch(branch or "") or ".." in branch:
        res["output"] = f"Refused: '{branch}' is not an agent/... branch."
    elif not origin:
        res["output"] = "No remote recorded for this job - local branch only."
    elif not _push_lock.acquire(blocking=False):
        res["output"] = "Another push is running - try again in a moment."
    else:
        try:
            r = subprocess.run([str(PUSH), str(work_dir), branch, origin],
                               capture_output=True, text=True, timeout=600)
            res["ok"] = r.returncode == 0
            res["output"] = (r.stdout + r.stderr).strip()[-4000:]
        except Exception as ex:
            res["output"] = f"Push failed to run: {ex}"
        finally:
            _push_lock.release()
    m["push"] = res
    return res


# ------------------------------------------------------------------ HTML

def esc(s):
    return html.escape(str(s if s is not None else ""))


def badge_html(m):
    if not is_local(m):
        return ""
    return ('<span class="jailbadge" title="No internet, no GitHub or Claude login, only this project folder. '
            'The sandbox self-test passed before the job started.">&#128274; Sandbox: on (no internet)</span>')


BADGE_CSS = ('<style>.jailbadge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:12px;'
             'border:1px solid var(--ok);color:var(--ok);margin-right:6px}</style>')


def push_card_html(m, jid, in_sandbox, readonly=False):
    if m.get("kind") == "bench" or m.get("status") not in ("done", "failed", "stopped", "interrupted"):
        return ""
    last = m.get("push")
    last_html = ""
    if last:
        cls = "mut" if last.get("ok") else "bad"
        when = time.strftime("%d %b %H:%M", time.localtime(last.get("time", 0)))
        head = (f'Pushed {esc(last.get("branch"))} at {when}' if last.get("ok")
                else f'Push of {esc(last.get("branch"))} failed at {when}')
        last_html = f'<p><b>{head}</b></p><pre class="{cls}">{esc(last.get("output"))}</pre>'
    if "git_origin" not in m:
        msg = '<p class="mut">No remote recorded (this job ran before the sandbox upgrade). Push it by hand, or run a short follow-up job so the board records the remote.</p>'
        return f'<div class="card"><h2>Push branch</h2>{msg}{last_html}</div>'
    if not m.get("git_origin"):
        why = (f' (origin <code>{esc(m.get("git_origin_raw"))}</code> is not GitHub or a local path, so the board will not push to it)'
               if m.get("git_origin_raw") else "")
        return (f'<div class="card"><h2>Push branch</h2><button disabled>No remote &mdash; local branch only</button>'
                f'<p class="hint">{why.strip() or "The project had no origin remote when the job started."}</p>{last_html}</div>')
    branches, cur = agent_branches(m, in_sandbox)
    if not branches:
        return (f'<div class="card"><h2>Push branch</h2><button disabled>No agent/... branch to push</button>'
                f'<p class="hint">Only branches named agent/... are pushed.</p>{last_html}</div>')
    opts = "".join(f'<option value="{esc(b)}"{" selected" if b == cur else ""}>{esc(b)}</option>' for b in branches)
    dis = " disabled" if readonly else ""
    return (f'<div class="card"><h2>Push branch</h2><form method="post" action="/push/{esc(jid)}">'
            f'<div class="brow"><select name="branch">{opts}</select><button{dis}>Push branch</button></div>'
            f'<p class="hint">Pushes to <code>{esc(m["git_origin"])}</code> (the remote recorded when the job started). '
            f'Copied through a clean mirror; the job\'s own repo settings and hooks are never used. Only agent/... branches.</p>'
            f'</form>{last_html}</div>')


OFFLINE_FIELD = ('<label style="font-weight:400"><input type="checkbox" name="offline" id="offlinebox"> '
                 'Offline: the sandbox can reach only the local model (no downloads; not for Supervised)</label>'
                 '<p class="hint" id="offlinenote" hidden>&#128274; Local AI jobs always run with no internet.</p>')

# Locks the Offline box on for local modes. Runs after the draft restore on the home page.
OFFLINE_JS = """<script>
(function(){
const f=document.querySelector('form[action="/new"]');if(!f)return;
const sel=f.elements.mode,box=document.getElementById('offlinebox'),note=document.getElementById('offlinenote');
const LOCAL=%s;let mine=box.checked;
function sync(){const loc=LOCAL.includes(sel.value);
 if(loc){box.checked=true;box.disabled=true;note.hidden=false}
 else{box.disabled=false;note.hidden=true;if(sel.value==='supervised')box.checked=false;else box.checked=mine}}
box.addEventListener('change',()=>{mine=box.checked});sel.addEventListener('change',sync);setTimeout(sync,0);sync();
})();
</script>""" % ('["' + '","'.join(LOCAL_MODES) + '"]')


# ------------------------------------------------------------------ download packages
# JAIL (2026-11-11): the AI never gets internet; the harness downloads. jail/fetch-deps.sh
# runs in a separate container with internet but no logins/model, only the folder + caches.

FETCH = JAIL / "fetch-deps.sh"
FETCH_RUNS = HOME / "agent-sandbox" / "state" / "fetch-runs"
FETCH_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")
FOLDER_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_fetch_lock = threading.Lock()
_fetching = set()                       # folders with a download running


def _fmeta(rid):
    import json
    try: return json.loads((FETCH_RUNS / rid / "meta.json").read_text())
    except Exception: return None


def _fsave(rid, m):
    import json
    p = FETCH_RUNS / rid / "meta.json"; t = p.with_suffix(".tmp")
    t.write_text(json.dumps(m, indent=1)); t.replace(p)


def run_fetch_sync(folder, log_path, timeout=960):
    """Run fetch-deps.sh, write its output to log_path; returns exit code (or 1)."""
    with _fetch_lock:
        if folder in _fetching:
            Path(log_path).write_text("A download for this folder is already running.\n"); return 1
        _fetching.add(folder)
    try:
        with open(log_path, "wb") as out:
            r = subprocess.run([str(FETCH), folder], stdin=subprocess.DEVNULL, stdout=out,
                               stderr=subprocess.STDOUT, timeout=timeout)
        return r.returncode
    except Exception as ex:
        with open(log_path, "a") as f: f.write(f"\n(download failed to run: {ex})\n")
        return 1
    finally:
        with _fetch_lock: _fetching.discard(folder)


def start_fetch(folder, work_dir, job_id=None):
    """Start a download in the background; returns the run id (or None if refused)."""
    import secrets
    if not FOLDER_OK.fullmatch(folder or "") or folder == "eval" or not (work_dir / folder).is_dir():
        return None
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    (FETCH_RUNS / rid).mkdir(parents=True)
    m = {"id": rid, "folder": folder, "job": job_id, "status": "running", "started": time.time(),
         "finished": None, "exit": None}
    _fsave(rid, m)

    def go():
        rc = run_fetch_sync(folder, FETCH_RUNS / rid / "output.log")
        m.update(status="done" if rc == 0 else "failed", finished=time.time(), exit=rc); _fsave(rid, m)
    threading.Thread(target=go, daemon=True).start()
    return rid


def fetch_runs_for(folder, n=3):
    if not FETCH_RUNS.is_dir():
        return []
    out = []
    for d in sorted(FETCH_RUNS.iterdir(), reverse=True):
        m = _fmeta(d.name)
        if m and m.get("folder") == folder:
            out.append(m)
            if len(out) >= n: break
    return out


def fetch_page_body(rid):
    if not FETCH_ID_RE.fullmatch(rid or ""):
        return None
    m = _fmeta(rid)
    if not m:
        return None
    try: out = (FETCH_RUNS / rid / "output.log").read_text(errors="replace")[-60000:]
    except OSError: out = ""
    running = m["status"] == "running"
    state = {"running": "Downloading...", "done": "&#10003; Download OK", "failed": "&#10007; Download failed"}[m["status"]]
    back = f'<a href="/job/{esc(m["job"])}">Back to the job</a>' if m.get("job") else '<a href="/">Back</a>'
    refresh = '<meta http-equiv="refresh" content="3">' if running else ""
    return (f'{refresh}<div class="card"><h2>Download packages: {esc(m["folder"])}</h2>'
            f'<p><b>{state}</b> &middot; {back}</p>'
            f'<p class="hint">Runs in a separate sandbox with internet but no logins and no AI, seeing only this folder and the shared '
            f'package caches. Gradle still reads the project\'s build files to find the packages.</p>'
            f'<pre>{esc(out) or "Starting..."}</pre></div>')


def fetch_card_html(m, jid, readonly=False):
    if m.get("kind") == "bench":
        return ""
    folder = m.get("folder") or ""
    rows = "".join(f'<li><a href="/fetch/{esc(r["id"])}">{time.strftime("%d %b %H:%M", time.localtime(r["started"]))}</a> '
                   f'{"running" if r["status"] == "running" else ("OK" if r["status"] == "done" else "failed")}</li>'
                   for r in fetch_runs_for(folder))
    log = ""
    if m.get("fetch_first"):
        log = f'<p class="mut">This job asked to download packages first: {esc(m.get("fetch_result", "pending"))}.</p>'
    dis = " disabled" if readonly or m.get("status") in ("running", "stopping") else ""
    return (f'<div class="card"><h2>Packages</h2><form method="post" action="/fetch">'
            f'<input type="hidden" name="folder" value="{esc(folder)}"><input type="hidden" name="job" value="{esc(jid)}">'
            f'<button class="ghost"{dis}>Download packages</button></form>'
            f'<p class="hint">Local agents have no internet. This downloads the folder\'s Gradle / npm / pip packages into the '
            f'shared cache (separate sandbox, no logins, no AI), so the next offline job can build.</p>'
            f'{log}{"<ul>" + rows + "</ul>" if rows else ""}</div>')


FETCH_FORM_FIELD = ('<label style="font-weight:400"><input type="checkbox" name="fetch_first"> '
                    'Download packages before starting (Gradle / npm / pip, in a separate sandbox with internet but no AI)</label>')
FETCH_FORM_BUTTON = ('<button class="ghost" formaction="/fetch" formnovalidate '
                     'title="Download the selected folder\'s packages now, without starting a job">Download packages only</button>')
