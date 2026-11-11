#!/usr/bin/env python3
"""Upgrade 8: approve / merge / ask for changes / reject, pressed by a person only
(added 2026-11-12 by Claude; jobboard.py keeps small "# U08" hooks).

The merge / reject / undo logic is the given `agentjobs_upgrades/actions.py`
(`Decisions`, `merge_allowed`, `form_token`, `check_request`, `change_request_prompt`) -
it is not reimplemented here. This module is the glue the board needs:

  * WHICH branch: a job merges into the branch it started from (`git_branch_start`, written
    by `record_git_start` before the agent ran; main/master if the record has none), from
    the branch holding its own commits (`agent/<job id>` for a best-of-N winner, else the
    folder's current `agent/...` branch, else the newest `agent/...` branch that comes from
    this job's starting commit and is not already merged). Every git call goes through
    `upgrades.git_for` = `safegit.SafeGit` in that folder's own jail, so the repo's own
    (agent-planted) hooks and config are never trusted.
  * WHO may press it: a POST to /decide/<jid> must carry the `aj` HttpOnly session cookie
    AND the hidden form token that the server derives from a secret file
    (~/.local/state/agentjobs/secret, mode 600, env JOBBOARD_HUMAN_SECRET). That file is in
    none of the jail's mounts (see jail/podman-local.sh: only the project folder,
    state/home-local, state/android-sdk and two jail scripts), so a job running on this PC
    cannot read it and cannot forge a token. Anything else gets 403.
  * WHAT happens: the merge is local only - the board still only ever pushes agent/...
    branches (jailcfg.push_branch), so the page says "Not pushed". A conflict is aborted
    and leaves the repo exactly as it was, and the card offers a follow-up job that merges
    the target into the job branch and resolves it. "Ask for changes" and "Reject" reuse
    the follow-up job and the branch rename (agent/rejected/<id>, undoable for 14 days).

Every decision goes into the job record (`decision_log`) and the job's timeline.
Standard library only.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from pathlib import Path

import dashboard
import upgrades
from agentjobs_upgrades import actions
from agentjobs_upgrades.actions import Decisions

esc = dashboard.esc

SECRET_FILE = Path(os.environ.get("JOBBOARD_HUMAN_SECRET") or actions.DEFAULT_SECRET)
COOKIE = "aj"                                  # the decision-session cookie (HttpOnly)
TOKEN_FIELD = "ajtok"                          # the hidden form field it must match
ACTIONS = ("merge", "ask", "reject", "undo-merge", "undo-reject", "fix-conflict")
FINISHED = ("done", "failed", "stopped", "interrupted", "safety_stop")
UNDO_DAYS = 14
SID_RE = re.compile(r"[A-Za-z0-9_-]{16,80}")
SESSION_RE = re.compile(r"[A-Za-z0-9-]{8,80}")     # agent session ids, as jobboard checks them
SHA_RE = re.compile(r"[0-9a-f]{7,40}")
BR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
TITLE_MAX = 70
REQUEST_MAX = 4000
CARRY = ("checks", "checks_source", "verify_off", "fix_rounds")   # U01 fields a follow-up keeps


# ------------------------------------------------------------------ the human-only secret

_secret_cache = {}


def secret():
    """The secret in the file outside everything the jail can read (created 0600 once)."""
    key = str(SECRET_FILE)
    if key not in _secret_cache:
        _secret_cache[key] = actions.load_secret(SECRET_FILE)
    return _secret_cache[key]


def sid_from_cookie(cookie_hdr):
    """The browser's decision session id, or '' when there isn't a usable one."""
    mt = re.search(r"(?:^|;\s*)%s=([^;]+)" % COOKIE, cookie_hdr or "")
    sid = mt.group(1) if mt else ""
    return sid if SID_RE.fullmatch(sid) else ""


def new_sid():
    return secrets.token_urlsafe(24)


def cookie_header(sid):
    return f"{COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict"


def session_for(headers):
    """(session id, extra response headers) for a request: keep the browser's decision
    session, or start one so its forms can carry a token the secret file makes."""
    sid = sid_from_cookie(headers.get("Cookie", ""))
    if sid:
        return sid, []
    sid = new_sid()
    return sid, [("Set-Cookie", cookie_header(sid))]


def token_for(sid):
    """What the page puts in its forms. The secret itself is never sent to a browser."""
    return actions.form_token(secret(), sid) if sid else ""


def allowed(method, cookie_hdr, form_tok):
    """True only for a POST carrying the session cookie and that session's own token."""
    sid = sid_from_cookie(cookie_hdr)
    return actions.check_request(secret(), method, sid or None, (form_tok or "").strip() or None)


# ------------------------------------------------------------------ which branches

def title_of(m):
    """The job title for the merge message: its first line, short."""
    return " ".join(str(m.get("job") or "").split())[:TITLE_MAX] or "job"


def gate(m):
    """(gate status, plain words) - only 'passed' may merge without typing MERGE."""
    if m.get("verify_off"):
        return "off", "You told the board not to check this job, so nothing was verified."
    v = m.get("verify") or {}
    st = str(v.get("status") or "")
    if not st:
        return "unverified", "The checks were not run."
    words = {"passed": "All checks passed.", "unverified": "The checks were not run.",
             "needs_review": "The gate says this one needs a look.",
             "stuck": "The checks got stuck - they need a look.",
             "failed": "The checks failed.", "aborted": "The checks did not finish."}
    return st, words.get(st, "The checks gave no result.")


def heads(git):
    """[(branch name, last commit time)] for the local branches, names checked."""
    out = git.run("for-each-ref", "--format=%(refname:short)\t%(committerdate:unix)",
                  "refs/heads/", check=False)
    res = []
    for ln in out.splitlines():
        name, _, when = ln.partition("\t")
        name = name.strip()
        if BR_RE.fullmatch(name) and ".." not in name:
            res.append((name, int(when) if when.strip().isdigit() else 0))
    return res


def current_branch(git):
    br = git.run("rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    return br if (br != "HEAD" and BR_RE.fullmatch(br) and ".." not in br) else ""


def target_branch(m, git, hs=None):
    """The branch the job started from (the record's, else main/master). '' = none."""
    hs = hs if hs is not None else heads(git)
    names = [n for n, _ in hs]
    want = str(m.get("git_branch_start") or "")
    if BR_RE.fullmatch(want) and ".." not in want and want in names:
        return want
    for d in ("main", "master"):
        if d in names:
            return d
    return ""


def has_own_work(git, name, base, target):
    """Does this branch hold commits of its own that the target does not have yet?"""
    if target and git.ok("merge-base", "--is-ancestor", name, target):
        return False
    if base and not git.ok("merge-base", "--is-ancestor", base, name):
        return False
    return True


def job_branch(m, git, target, hs=None):
    """The branch with this job's commits ('' = nothing to merge).

    A best-of-N job's winner is named after the job, so that comes first. Otherwise the
    branches carrying this job's own checkpoint commits (refs the board wrote while it ran)
    are the sure ones; only when the record has no checkpoints to go on does the board fall
    back to the branch the folder is on, or the newest one that starts from this job's base
    commit - and the card always names the branch it means to merge."""
    hs = hs if hs is not None else heads(git)
    dated = dict(hs)
    jid = str(m.get("id") or "")
    cand = [n for n, _ in hs
            if n.startswith("agent/") and n != target and not n.startswith("agent/rejected/")
            and not n.startswith(f"agent/{jid}-try")]
    cand.sort(key=lambda n: -dated[n])
    cand = cand[:8]                    # each candidate costs a git call in the jail, so keep it small
    base = str(m.get("git_start") or "")
    good = [n for n in cand if has_own_work(git, n, base, target)]
    if not good:
        return ""
    marked = [n for n in good if carries_checkpoints(git, m, n)]
    pool = marked or good
    winner = f"agent/{jid}"                       # upgrade 5 puts the best-of-N winner here
    if winner in pool:
        return winner
    cur = current_branch(git)
    if cur in pool:
        return cur
    return max(pool, key=lambda n: dated[n])


def carries_checkpoints(git, m, name):
    """True when one of this job's checkpoint commits is on that branch."""
    for c in (m.get("checkpoints") or {}).get("checkpoints", [])[:6]:
        sha = str(c.get("commit") or "")
        if SHA_RE.fullmatch(sha) and git.ok("merge-base", "--is-ancestor", sha, name):
            return True
    return False


_hint_cache = {}                       # job id -> (time, target, branch): one jail run per page is enough
HINT_TTL = 30


def branch_hint(m, root, ttl=HINT_TTL):
    """(target branch, job branch) as the project sees them, read in the job's own jail.
    ('', '') when the folder is not a git repository or git could not run."""
    jid = str(m.get("id") or "")
    now = time.time()
    hit = _hint_cache.get(jid)
    if hit and now - hit[0] < ttl:
        return hit[1], hit[2]
    target = branch = ""
    try:
        if upgrades.git_repo(root):
            git = upgrades.git_for(m, Path(root))
            hs = heads(git)
            target = target_branch(m, git, hs)
            branch = job_branch(m, git, target, hs)
    except Exception:
        target = branch = ""
    _hint_cache[jid] = (now, target, branch)
    return target, branch


# ------------------------------------------------------------------ the record and the timeline

def state(m):
    """What the last decisions left: merged / conflict / rejected / asked / refused."""
    out = {"merge": None, "conflict": None, "reject": None, "changes": None,
           "fix": None, "refused": None}
    for e in (m.get("decision_log") or []):
        k = e.get("kind")
        if k == "merge":
            out["merge"], out["conflict"] = e, None
        elif k == "undo-merge":
            out["merge"] = None
        elif k == "conflict":
            out["conflict"] = e
        elif k == "reject":
            out["reject"] = e
        elif k == "undo-reject":
            out["reject"] = None
        elif k == "changes":
            out["changes"] = e
        elif k == "fix-conflict":
            out["fix"] = e
        elif k == "refused":
            out["refused"] = e
        if k != "refused":
            out["refused"] = None
    return out


def _entry(log, kind, **kw):
    e = {"kind": kind, "time": time.time()}
    e.update(kw)
    log.append(e)
    return e


def _save(ctx, jid, log, decision=None):
    kw = {"decision_log": log}
    if decision is not None:
        kw["decision"] = decision
    _hint_cache.pop(str(jid), None)                # the branches just changed, so look again
    ctx["update"](jid, **kw)


def _tl(ctx, jid, kind, text):
    dashboard.tl_add(ctx["job_dir"](jid), kind, text)


def _when(t):
    return time.strftime("%d %b %H:%M", time.localtime(t)) if t else ""


def _followup(m, ctx, prompt):
    """A follow-up job in the same folder and branch, keeping the U01 fields."""
    extra = {k: m[k] for k in CARRY if k in m}
    sid = m.get("session")
    return ctx["new_job"](m["mode"], m["folder"], prompt, bool(m.get("offline", False)),
                          resume=sid if isinstance(sid, str) and SESSION_RE.fullmatch(sid) else None,
                          parent=m["id"], extra=extra or None)


def fix_conflict_prompt(m, target, branch):
    return ("A merge Jason asked for hit conflicts, so the clash has to be settled on your branch.\n\n"
            f"The job: {str(m.get('job') or '')[:4000]}\n\n"
            f"Jason asked to merge {branch} into {target}. The two changed the same lines, so the "
            "board aborted the merge and changed nothing.\n\n"
            "What to do:\n"
            f"1. Stay on {branch} and merge {target} into it.\n"
            f"2. Resolve every conflict, keeping both what {target} has and what this job adds.\n"
            "3. Run the project's checks and make them pass.\n"
            f"4. Commit the merge on {branch} and write in AGENT_REPORT.md what you settled.\n\n"
            f"Do not merge into {target} yourself, and never push - the board does the merge.")


# ------------------------------------------------------------------ the actions

def _repo(m, ctx):
    """(git, root) for the job's project folder - git in the folder's own jail - or
    (None, why not)."""
    root = ctx["work"] / m["folder"]
    if not upgrades.git_repo(root):
        return None, "That folder is not a git repository, so there is nothing to merge."
    return (upgrades.git_for(m, root), root), ""


def _merge_plan(m, ctx):
    """(git, root, target, branch) for a merge or a reject, or (None, why not)."""
    r, why = _repo(m, ctx)
    if not r:
        return None, why
    git, root = r
    hs = heads(git)
    target = target_branch(m, git, hs)
    if not target:
        return None, "The project has no branch to merge into."
    branch = job_branch(m, git, target, hs)
    if not branch:
        extra = ""
        base = str(m.get("git_start") or "")
        if SHA_RE.fullmatch(base):
            try:
                ahead = int(git.run("rev-list", "--count", f"{base}..{target}", check=False).strip() or 0)
            except ValueError:
                ahead = 0
            if ahead:
                extra = f" {target} already holds the {ahead} commit(s) this folder got since."
        return None, (f"There is nothing to merge into {target} - this job left no branch "
                      f"with changes of its own.{extra}")
    return (git, root, target, branch), ""


def do_merge(m, get, ctx):
    jid = m["id"]
    log = list(m.get("decision_log") or [])
    status, _words = gate(m)
    ok, why = actions.merge_allowed(status, get("typed"))
    if not ok:
        _entry(log, "refused", reason=why)
        _save(ctx, jid, log)
        return
    plan, why = _merge_plan(m, ctx)
    if not plan:
        _entry(log, "refused", reason=why)
        _save(ctx, jid, log)
        return
    git, root, target, branch = plan
    res = Decisions(root, git=git).merge(jid, branch, target, title_of(m))
    if not res.get("ok"):
        if res.get("conflict"):
            _entry(log, "conflict", target=target, branch=branch,
                   reason=res.get("reason", ""), detail=str(res.get("detail") or "")[:500])
            _save(ctx, jid, log, "conflict")
            _tl(ctx, jid, "needs_review", f"Merge into {target} hit a conflict - nothing was changed")
        else:
            _entry(log, "refused", reason=res.get("reason", ""))
            _save(ctx, jid, log)
        return
    _entry(log, "merge", target=target, branch=branch, how=res["how"], before=res["before"],
           after=res["after"], pushed=False)
    _save(ctx, jid, log, "merged")
    _tl(ctx, jid, "merged", f"Merged {branch} into {target} ({res['how']}) - not pushed")


def do_undo_merge(m, get, ctx):
    jid = m["id"]
    log = list(m.get("decision_log") or [])
    last = state(m)["merge"]
    if not last:
        return
    r, why = _repo(m, ctx)
    if not r:
        _entry(log, "refused", reason=why)
        _save(ctx, jid, log)
        return
    git, root = r
    res = Decisions(root, git=git).undo_merge(last)
    if not res.get("ok"):
        _entry(log, "refused", reason=res.get("reason", ""))
        _save(ctx, jid, log)
        return
    last["undone"] = time.time()
    _entry(log, "undo-merge", target=last["target"], before=last["before"])
    _save(ctx, jid, log, "merge-undone")
    _tl(ctx, jid, "rollback", f"Merge undone - {last['target']} is back at {str(last['before'])[:8]}")


def do_reject(m, get, ctx):
    jid = m["id"]
    plan, why = _merge_plan(m, ctx)
    log = list(m.get("decision_log") or [])
    if not plan:
        _entry(log, "refused", reason=why)
        _save(ctx, jid, log)
        return
    git, root, _target, branch = plan
    res = Decisions(root, git=git).reject(jid, branch)
    _entry(log, "reject", **{k: v for k, v in res.items() if k != "ok"})
    _save(ctx, jid, log, "rejected")
    _tl(ctx, jid, "rejected", f"Rejected - the branch is now {res['branch']}")


def do_undo_reject(m, get, ctx):
    jid = m["id"]
    log = list(m.get("decision_log") or [])
    last = state(m)["reject"]
    if not last:
        return
    r, why = _repo(m, ctx)
    if not r:
        _entry(log, "refused", reason=why)
        _save(ctx, jid, log)
        return
    git, root = r
    res = Decisions(root, git=git).unreject(last, days=UNDO_DAYS)
    if not res.get("ok"):
        _entry(log, "refused", reason=res.get("reason", ""))
        _save(ctx, jid, log)
        return
    last["undone"] = time.time()
    _entry(log, "undo-reject", branch=last["old_branch"])
    _save(ctx, jid, log, "rejection-undone")
    _tl(ctx, jid, "restart", f"Rejection undone - the branch is back to {last['old_branch']}")


def do_ask(m, get, ctx):
    jid = m["id"]
    log = list(m.get("decision_log") or [])
    request = get("request").strip()[:REQUEST_MAX]
    if not request:
        _entry(log, "refused", reason="Write what you want changed first.")
        _save(ctx, jid, log)
        return
    child = _followup(m, ctx, actions.change_request_prompt(str(m.get("job") or ""), request))
    _entry(log, "changes", text=request, job=child["id"])
    _save(ctx, jid, log, "changes")
    _tl(ctx, jid, "changes", f"You asked for changes: {request[:120]}")


def do_fix_conflict(m, get, ctx):
    jid = m["id"]
    log = list(m.get("decision_log") or [])
    conf = state(m)["conflict"]
    if not conf:
        return
    child = _followup(m, ctx, fix_conflict_prompt(m, conf["target"], conf["branch"]))
    _entry(log, "fix-conflict", job=child["id"], target=conf["target"], branch=conf["branch"])
    _save(ctx, jid, log, "fix-conflict")
    _tl(ctx, jid, "fix_round", f"Asked Qwen to fix the clash between {conf['branch']} and {conf['target']}")


def action(jid, act, get, ctx):
    """The /decide/<jid> endpoint. The caller already checked the secret token and that the
    job is finished, its record is sound and nothing is running in that folder."""
    m = ctx["load"](jid)
    if not m or act not in ACTIONS:
        return
    {"merge": do_merge, "undo-merge": do_undo_merge, "reject": do_reject,
     "undo-reject": do_undo_reject, "ask": do_ask,
     "fix-conflict": do_fix_conflict}[act](m, get, ctx)


def refuse(jid, ctx, reason):
    """A decision that could not even be attempted (a repo that is not what the record says):
    say so on the card and the timeline instead of leaving Jason with a broken page."""
    m = ctx["load"](jid)
    if not m:
        return
    log = list(m.get("decision_log") or [])
    _entry(log, "refused", reason=str(reason)[:400])
    _save(ctx, jid, log)
    _tl(ctx, jid, "needs_review", f"That decision could not be made: {str(reason)[:200]}")


# ------------------------------------------------------------------ the card on the job page

def _form(jid, tok, act, extra="", button="Merge into main", cls=""):
    return (f'<form method="post" action="/decide/{esc(jid)}" style="display:inline">'
            f'<input type="hidden" name="{TOKEN_FIELD}" value="{esc(tok)}">'
            f'<input type="hidden" name="act" value="{act}">{extra}'
            f'<button{cls}>{button}</button></form>')


def card_html(m, jid, can_act, tok="", root=None):
    """The decide card: Merge into main / Ask for changes / Reject, plus what was decided
    already (and Undo). Without `can_act` nothing is offered, only what happened."""
    if m.get("kind") == "bench" or m.get("status") not in FINISHED:
        return ""
    st = state(m)
    status, words = gate(m)
    target = branch = ""
    if can_act and root is not None and not (st["merge"] or st["reject"]):
        target, branch = branch_hint(m, root)   # a job already merged or rejected has nothing to merge
    target = target or target_word(m)
    head = ""
    if st["merge"]:
        e = st["merge"]
        head += (f'<p class="ok">Merged <code>{esc(e["branch"])}</code> into '
                 f'<code>{esc(e["target"])}</code> ({esc(e["how"])}) on {_when(e["time"])}.</p>'
                 f'<p class="hint">Not pushed &mdash; the board only ever pushes agent/... '
                 f'branches, so pushing {esc(e["target"])} is yours to do.</p>')
        if can_act:
            head += _form(jid, tok, "undo-merge", button="Undo merge", cls=' class="ghost2"')
    if st["conflict"]:
        e = st["conflict"]
        head += (f'<p class="warn2">{esc(e.get("reason") or "The changes clash with the target branch.")} '
                 f'<span class="hint">{esc(e["branch"])} into {esc(e["target"])}, '
                 f'{_when(e["time"])}.</span></p>')
        if e.get("detail"):
            head += f'<details><summary class="hint">What git said</summary><pre class="bad">{esc(e["detail"])}</pre></details>'
        if can_act and not st["fix"]:
            head += _form(jid, tok, "fix-conflict", button="Ask Qwen to fix the conflict",
                          cls=' class="ghost2"')
    if st["fix"]:
        e = st["fix"]
        head += (f'<p class="hint">A job is sorting the clash out: '
                 f'<a href="/job/{esc(e["job"])}">{esc(e["job"])}</a></p>')
    if st["reject"]:
        e = st["reject"]
        head += (f'<p>Rejected on {_when(e["time"])} &mdash; the branch is now '
                 f'<code>{esc(e["branch"])}</code> and the folder is left as it is.</p>')
        if can_act and time.time() - e["time"] < UNDO_DAYS * 86400:
            head += _form(jid, tok, "undo-reject", button="Undo", cls=' class="ghost2"')
        elif not can_act or time.time() - e["time"] >= UNDO_DAYS * 86400:
            head += '<p class="hint">Undo is only possible for 14 days.</p>'
    if st["changes"]:
        e = st["changes"]
        head += (f'<p class="hint">You asked for changes on {_when(e["time"])}: '
                 f'&ldquo;{esc(e["text"][:200])}&rdquo; &mdash; '
                 f'<a href="/job/{esc(e["job"])}">the follow-up job</a></p>')
    if st["refused"]:
        head += f'<p class="warn2">{esc(st["refused"].get("reason") or "")}</p>'

    buttons = ""
    if can_act:
        merge_btn = ""
        if st["merge"] or st["reject"]:
            merge_btn = ""
        elif not branch:
            merge_btn = (f'<button disabled title="Nothing to merge">Merge into {esc(target)}</button>'
                         '<p class="hint">Nothing to merge &mdash; this job left no branch with '
                         'changes of its own.</p>')
        elif actions.merge_allowed(status)[0]:
            merge_btn = _form(jid, tok, "merge", button=f"Merge into {esc(target)}")
        else:
            merge_btn = (f'<button disabled title="{esc(words)}">Merge into {esc(target)}</button>'
                         f'<details class="mergeany"><summary class="hint">Merge anyway</summary>'
                         + _form(jid, tok, "merge", extra='<input type="text" name="typed" '
                                 'placeholder="Type MERGE" style="margin-right:6px">',
                                 button="Merge anyway", cls=' class="ghost2"') + '</details>')
        buttons = (f'<div class="brow">{merge_btn}</div>'
                   + (f'<p class="hint">Merges <code>{esc(branch)}</code> into '
                      f'<code>{esc(target)}</code> in the project folder, right here on this '
                      f'PC. Nothing is pushed.</p>' if branch else '')
                   + f'<form method="post" action="/decide/{esc(jid)}">'
                   f'<input type="hidden" name="{TOKEN_FIELD}" value="{esc(tok)}">'
                   f'<input type="hidden" name="act" value="ask">'
                   f'<textarea name="request" placeholder="What should Qwen change? This starts a '
                   f'follow-up job on the same branch with your words and the original task."></textarea>'
                   f'<button class="ghost2">Ask for changes</button></form> '
                   + _form(jid, tok, "reject", button="Reject", cls=' class="ghost"')
                   + '<p class="hint">Reject keeps the folder and renames the branch to '
                     'agent/rejected/&lt;this job&gt;. Undo works for 14 days.</p>')
    return (f'<div class="card" id="decidecard"><h2>Approve or reject</h2>'
            f'<p class="hint">{esc(words)}</p>{head}{buttons}</div>')


def target_word(m):
    """The branch name to show when git could not be asked (read-only copies, old records)."""
    want = str(m.get("git_branch_start") or "")
    if BR_RE.fullmatch(want) and ".." not in want:
        return want
    return "main"


CSS = """<style>
#decidecard .brow{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:8px 0}
#decidecard details.mergeany{display:inline-block}
#decidecard textarea{min-height:70px}
#decidecard button[disabled]{opacity:.45;cursor:not-allowed}
</style>"""
