#!/usr/bin/env python3
"""Job board: a local web page for the sandboxed coding agent.

Start:  python3 ~/agent-sandbox/web/jobboard.py      then open the link it prints.
Listens on 127.0.0.1:8090 only. Every request needs the secret key from the printed link
(kept as a cookie), so other websites open in the browser cannot start jobs.

Jobs run one at a time (the GPU serves one stream) through ~/agent-sandbox/agent, inside the
rootless Podman sandbox. Job records live in ~/agent-sandbox/state/jobs/<id>/ - OUTSIDE the
folders the sandbox mounts (~/agent-work and the state/home + state/android-sdk mounts), so
agents inside the sandbox cannot write them. Standard library only.
"""
import html, http.server, json, os, re, secrets, shutil, signal, subprocess, sys, threading, time, urllib.parse, urllib.request
from pathlib import Path

import benchpages
import benchstats
import jailcfg   # JAIL: no-internet sandbox for local modes, self-test, Push button
import jobstatus
import reviewbundle

HOST, PORT = "127.0.0.1", int(os.environ.get("JOBBOARD_PORT", "8090"))
HOME = Path.home()
# Paths and mode are overridable via env for tests and read-only copies.
# JOBS deliberately does NOT live under WORK: the sandbox mounts ~/agent-work
# read-write, so records there could be forged by any agent. JOBBOARD_JOBS
# still overrides the location (tests, read-only copies). Nothing moves the
# old WORK/.jobs automatically; startup just notices and says so.
WORK = Path(os.environ.get("JOBBOARD_WORK") or (HOME / "agent-work"))
SANDBOX = HOME / "agent-sandbox"
JOBS = Path(os.environ.get("JOBBOARD_JOBS") or (SANDBOX / "state" / "jobs"))
READONLY = os.environ.get("JOBBOARD_READONLY", "") == "1"
AGENT = SANDBOX / "agent"
IMAGE = "localhost/local-agent:latest"
TOKEN_FILE = SANDBOX / "state" / "web-token"
MODES = {
    "local-claude": "Local: Claude Code on the local model (no cloud)",
    "local-qwen": "Local: Qwen Code on the local model (no cloud)",
    "supervised": "Supervised: Claude plans and reviews, local model codes",
}
BUILD_EXT = (".apk", ".aab", ".exe", ".msi", ".msix", ".appimage", ".deb", ".zip", ".ipa", ".dmg")
SKIP_DIRS = {".git", "node_modules", ".gradle", ".venv", "__pycache__", ".idea", ".cxx"}
FOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
JOB_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")       # what new_job generates
SESSION_RE = re.compile(r"[A-Za-z0-9-]{8,80}")           # agent session ids (resume)
JOB_TEXT_MAX = 200000                                    # same cap as the web form read
# Bench auto-runs. Run data lives OUTSIDE the sandbox-visible folders; the worker
# reads the run settings ONLY from BENCH_RUNS/<id>/spec.json (never from meta.json).
# Job records also live outside the sandbox now, but they are plain files, so the
# worker re-checks every queued record right before running it (check_record).
# These are read through the module globals so tests can point them at
# bench-ref / a fake server.
BENCH_RUNS = Path(os.environ.get("BENCH_RUNS") or (HOME / "bench" / "autoruns"))
BENCH_DIR = os.environ.get("BENCH_DIR") or str(HOME / "bench")
BENCH_URL = os.environ.get("BENCH_URL") or "http://127.0.0.1:8080/v1"
BENCH_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")

JOBS.mkdir(parents=True, exist_ok=True)
TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(24))
    TOKEN_FILE.chmod(0o600)
TOKEN = TOKEN_FILE.read_text().strip()

lock = threading.Lock()
queue_cv = threading.Condition(lock)
running = {}  # job id -> Popen


# ---------------------------------------------------------------- jobs

def job_dir(jid): return JOBS / jid

def load_meta(jid):
    try: return json.loads((job_dir(jid) / "meta.json").read_text())
    except Exception: return None

def save_meta(m, jid=None):
    """Write meta.json. jid = the record's DIRECTORY name; it defaults to m["id"],
    but a tampered record's "id" field may differ, so the worker always passes the
    real directory name when saving a record it did not create."""
    p = job_dir(jid if jid is not None else m["id"]) / "meta.json"
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(m, indent=1)); tmp.replace(p)

def all_job_dirs():
    """(directory name, meta) for every record, newest first. The worker queues
    by DIRECTORY name, never by the meta's "id" field (a tampered record can lie
    about its id; pages only ever display the metas)."""
    out = []
    for d in sorted(JOBS.iterdir(), reverse=True):
        m = load_meta(d.name)
        if m: out.append((d.name, m))
    return out

def all_jobs():
    return [m for _, m in all_job_dirs()]

def check_record(name, m):
    """The worker re-checks every queued record right before running it: records
    are plain files, so a check at POST time is not enough. Returns None when the
    record is runnable, else the field that failed (the caller marks the job
    failed with a "Refused: ..." note and never runs it)."""
    if not JOB_ID_RE.fullmatch(name or "") or m.get("id") != name:
        return "id"
    if m.get("kind") == "bench":
        if m.get("mode") != "bench":
            return "mode"
        run = bench_run_dir(name)          # validates the id format before the path
        if not run or not (run / "spec.json").exists():
            return "spec"
        return None
    if m.get("mode") not in MODES:
        return "mode"
    folder = m.get("folder")
    if not isinstance(folder, str) or not FOLDER_RE.fullmatch(folder) or folder == "eval":
        return "folder"
    job = m.get("job")
    if not isinstance(job, str) or not job or len(job) >= JOB_TEXT_MAX:
        return "job"
    if not isinstance(m.get("offline"), bool):
        return "offline"
    resume = m.get("resume")
    if resume is not None and (not isinstance(resume, str) or not SESSION_RE.fullmatch(resume)):
        return "resume"
    parent = m.get("parent")
    if parent is not None and (not isinstance(parent, str) or not JOB_ID_RE.fullmatch(parent)
                               or not load_meta(parent)):
        return "parent"
    return None

def new_job(mode, folder, job, offline, resume=None, parent=None):
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    job_dir(jid).mkdir(parents=True)
    (WORK / folder).mkdir(parents=True, exist_ok=True)
    if mode in jailcfg.LOCAL_MODES: offline = True   # JAIL: local AI never gets internet
    m = {"id": jid, "mode": mode, "folder": folder, "job": job, "offline": offline, "status": "queued",
         "created": time.time(), "started": None, "finished": None, "exit": None,
         "resume": resume, "parent": parent, "session": None}
    save_meta(m)
    with queue_cv: queue_cv.notify()
    return m

def worker():
    # Mark jobs that were running when the board last stopped.
    for name, m in all_job_dirs():
        if m.get("status") == "running":
            m.update(status="interrupted", finished=time.time()); save_meta(m, name)
    while True:
        with queue_cv:
            while True:
                q = [name for name, m in reversed(all_job_dirs()) if m["status"] == "queued"]
                if q: break
                queue_cv.wait(5)
        process_job(q[0])

def process_job(name):
    """Take one queued record (by directory name) through to finished. The
    safety re-check happens here, right before anything runs: a record that
    fails it is marked failed with the reason and never started."""
    m = load_meta(name)
    if not m:
        return False  # deleted (moved to .trash) while it sat in the queue
    problem = check_record(name, m)
    if problem:
        m.update(status="failed", finished=time.time(),
                 fail_note=f"Refused: the job record failed the safety check ({problem})")
        save_meta(m, name)
        return False
    if m.get("kind") == "bench":
        # mark it running BEFORE starting the runner: Stop and the live pills
        # need to see "running" while benchrun is in flight (and a board
        # restart then correctly marks it interrupted).
        m.update(status="running", started=time.time()); save_meta(m, name)
        rc = run_bench(m)
        m = load_meta(name)
        if not m:
            return False
        if m["status"] == "stopping":
            m["status"] = "stopped"
        else:
            m["status"] = "done" if rc == 0 else "failed"
        if m["status"] == "failed" and not m.get("fail_note"):
            m["fail_note"] = ("Basalt stopped answering - press Resume when it is back"
                              if rc == 3 else f"benchrun exited {rc}")
        m.update(finished=time.time(), exit=rc); save_meta(m, name)
        try:
            notify_finished(m, m["status"] == "done")
        except Exception:
            pass
        return True
    if jailcfg.is_local(m):   # JAIL: prove the sandbox is tight before a local job starts
        ok, out = jailcfg.selftest()
        (job_dir(name) / "sandbox-check.txt").write_text(out + "\n")
        if not ok:
            m.update(status="failed", finished=time.time(), fail_note=jailcfg.selftest_failure_note(out))
            save_meta(m, name)
            try: notify_finished(m, False)
            except Exception: pass
            return False
    cmd = [str(AGENT), m["mode"], "--stream", "--name", "agentjob-" + m["id"], "--in", m["folder"]]
    if m["offline"]: cmd.insert(2, "--offline")
    if m.get("resume"): cmd += ["--resume", m["resume"]]
    cmd.append(m["job"])
    m.update(status="running", started=time.time()); save_meta(m, name)
    record_git_start(m)
    with open(job_dir(name) / "events.jsonl", "wb") as out:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=str(HOME))
        running[m["id"]] = p
        rc, finished_ok = wait_with_watchdog(m["id"], p)
    running.pop(m["id"], None)
    m = load_meta(name)
    if m["status"] == "stopping": m["status"] = "stopped"
    elif finished_ok is not None: m["status"] = "done" if finished_ok else "failed"
    else: m["status"] = "done" if rc == 0 else "failed"
    m.update(finished=time.time(), exit=rc, session=session_of(m["id"]) or m.get("resume")); save_meta(m, name)
    try:
        notify_finished(m, m["status"] == "done")
    except Exception:
        pass
    try:  # review bundle saved with the job, so Copy/Download never wait
        build_review_md(m)
    except Exception:
        pass
    return True

SHA_RE = re.compile(r"[0-9a-f]{7,40}")

def record_git_start(m):
    """Remember the project's starting commit (and branch) right before the job
    begins, so a review bundle can diff exactly this job's changes. Only for git
    folders; never raises: git_start is a nicety, not a reason to stop a job."""
    try:  # JAIL: remember where agent/... branches may be pushed, read BEFORE the agent runs
        jailcfg.record_origin(m, in_sandbox); save_meta(m)
    except Exception:
        pass
    try:
        sha = in_sandbox(m["folder"], "git rev-parse --verify HEAD 2>/dev/null", timeout=30).strip()
        if not SHA_RE.fullmatch(sha):
            return
        m["git_start"] = sha
        br = in_sandbox(m["folder"], "git branch --show-current 2>/dev/null", timeout=30).strip()
        if br and re.fullmatch(r"[A-Za-z0-9._/-]{1,80}", br):
            m["git_branch_start"] = br
        save_meta(m)
    except Exception:
        pass

SOUNDS = {True: "/usr/share/sounds/freedesktop/stereo/complete.oga",
          False: "/usr/share/sounds/freedesktop/stereo/dialog-error.oga"}

def notify_finished(meta, ok):
    """Desktop notification + sound for a finished job (Jason's Ubuntu box).
    The board runs as a systemd --user service, so the session bus / runtime dir
    may be missing from the environment; fill in the usual per-uid paths.
    Never raises: a missing notify-send or sound player, or a display-less
    session, must not take down the worker thread."""
    title = "Agent job finished" if ok else "Agent job failed"
    text = " ".join(str((meta or {}).get("job") or (meta or {}).get("id") or "job").split())[:100]
    if (meta or {}).get("kind") == "bench":
        title = "Bench run finished" if ok else "Bench run failed"
        score = bench_overall_text(meta["id"])  # label + overall score (never raises)
        if score:
            text = f"{text} - {score}"
    env = dict(os.environ)
    try:
        uid = os.getuid()
    except Exception:
        uid = None
    if uid is not None:
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    cmds = [["notify-send", title, text]]
    snd = SOUNDS[bool(ok)]  # PipeWire (pw-play) first: paplay is not installed on PipeWire-only Ubuntu
    for player in ("pw-play", "paplay", "canberra-gtk-play"):
        if shutil.which(player):
            cmds.append(([player, "-f"] if player == "canberra-gtk-play" else [player]) + [snd])
            break
    for cmd in cmds:
        try:
            subprocess.run(cmd, env=env, capture_output=True, timeout=15)
        except Exception:
            pass

def build_review_md(m):
    """Build the review bundle right after an agent job ends and save it to
    JOBS/<id>/review.md, so Copy for Claude / Download serve instantly without
    Jason ever pressing a build button. Bench runs have no bundle; read-only
    copies never write. Callers wrap this whole call in try/except: a bundle
    failure must never break the worker thread."""
    if not m or m.get("kind") == "bench":
        return
    md = reviewbundle.build_bundle(
        m, job_dir(m["id"]) / "events.jsonl",
        lambda s: in_sandbox(m["folder"], s, timeout=120), load_meta)
    if not READONLY:
        (job_dir(m["id"]) / "review.md").write_text(md, encoding="utf-8")

def saved_review_md(m):
    """The auto-built review.md, but only when it is NEWER than the job's finish
    time (an older file may predate a re-run or a fixed record). None = build."""
    try:
        if not m:
            return None
        p = job_dir(str(m.get("id") or "")) / "review.md"
        if p.exists() and p.stat().st_mtime > float(m.get("finished") or 0):
            return p.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    return None

def status_for(m):
    """Live status of a job (state, what it is doing now, step checklist)."""
    if m.get("kind") == "bench":
        return bench_status(m)
    return jobstatus.job_status(m, job_dir(m["id"]) / "events.jsonl", WORK / (m.get("folder") or ""), time.time())

def session_of(jid):
    """The agent's session id from its event stream, so a follow-up can continue the same conversation."""
    sid = None
    try:
        for line in open(job_dir(jid) / "events.jsonl", encoding="utf-8", errors="replace"):
            if '"session_id"' in line:
                try: sid = json.loads(line).get("session_id") or sid
                except ValueError: pass
    except OSError: pass
    return sid if sid and SESSION_RE.fullmatch(sid) else None

GRACE_AFTER_RESULT = 120  # seconds an agent may linger after its final answer before it is stopped

def wait_with_watchdog(jid, p):
    """Wait for the agent. Once it has printed its final result, give it GRACE_AFTER_RESULT seconds to exit,
    then stop its container: unattended Claude Code can otherwise wait forever on a leftover background task.
    Returns (exit code, True/False from the final result if the watchdog stopped it, else None)."""
    result_at, result_ok = None, None
    ev = job_dir(jid) / "events.jsonl"
    while p.poll() is None:
        time.sleep(10)
        if result_at is None:
            try:
                tail = ev.read_bytes()[-20000:].decode("utf-8", "replace")
            except OSError:
                continue
            for line in tail.splitlines():
                if line.startswith('{"type":"result"'):
                    try: result_ok = not json.loads(line).get("is_error")
                    except ValueError: result_ok = True
                    result_at = time.time()
        elif time.time() - result_at > GRACE_AFTER_RESULT:
            subprocess.run(["podman", "stop", "-t", "10", "agentjob-" + jid], capture_output=True, timeout=60)
            return p.wait(), result_ok
    return p.returncode, None

def stop_job(jid):
    m = load_meta(jid)
    if not m: return
    if m["status"] == "queued":
        m.update(status="cancelled", finished=time.time()); save_meta(m); return
    if m["status"] == "running":
        m["status"] = "stopping"; save_meta(m)
        if m.get("kind") == "bench":
            # Stop = terminate the runner (it saves every finished sample already);
            # give it 15 s to exit on its own, then kill.
            p = running.get(jid)
            if p and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    p.kill()
                except Exception:
                    pass
        else:
            subprocess.run(["podman", "stop", "-t", "10", "agentjob-" + jid], capture_output=True, timeout=60)
            jailcfg.stop_children(jid)   # JAIL: offline local agents a supervised job started


# ---------------------------------------------------------------- bench auto-runs

def read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def bench_run_dir(jid):
    """Path for a run's data; the id format is validated BEFORE building any path."""
    if not BENCH_ID_RE.fullmatch(jid or ""):
        return None
    return BENCH_RUNS / jid

def bench_spec(jid):
    d = bench_run_dir(jid)
    return read_json(d / "spec.json") if d else None

def bench_samples(jid):
    d = bench_run_dir(jid)
    if not d: return []
    out = []
    try:
        for line in (d / "samples.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line: continue
            try: out.append(json.loads(line))
            except ValueError: pass
    except OSError: pass
    return out

def bench_progress(jid):
    d = bench_run_dir(jid)
    return read_json(d / "progress.json") if d else None

def bench_env(jid):
    d = bench_run_dir(jid)
    return read_json(d / "env.json") if d else None

def basalt_loaded(url):
    """True if the bench server answers /models (status 'loaded' or unknown)."""
    try:
        d = json.load(urllib.request.urlopen(url.rstrip("/") + "/models", timeout=5))
        return (d["data"][0].get("status") or {}).get("value", "loaded") == "loaded"
    except Exception:
        return False

def bench_task_secs():
    """Average seconds per answer per task, from every saved run; defaults are
    240 s hard / 90 s main / 400 s agent when no finished sample exists yet."""
    acc = {}
    try:
        dirs = [d for d in BENCH_RUNS.iterdir() if d.is_dir() and BENCH_ID_RE.fullmatch(d.name)]
    except OSError:
        dirs = []
    for d in dirs:
        for r in bench_samples(d.name):
            t = r.get("total_s")
            if isinstance(t, (int, float)) and t > 0 and r.get("task"):
                acc.setdefault(r["task"], []).append(t)
    out = {}
    for kind, ids in bench_catalog().items():
        default = {"hard": 240, "main": 90, "agent": 400}[kind]
        for t in ids:
            vals = acc.get(t)
            out[t] = round(sum(vals) / len(vals)) if vals else default
    return out

HARD_DEFAULT = ["h1_lisp", "h2_cron", "h3_patch", "h4_bugfix", "h5_puzzle"]
MAIN_DEFAULT = ["p1_expr", "p2_lru_ttl", "p3_toposort", "p4_regex", "p5_calendar",
                "p6_grid_path", "b1_billing", "longctx_30k", "longctx_100k"]

def bench_catalog():
    """Task ids found in BENCH_DIR (hard/..., suite/code/..., ...), with the
    known set as a fallback if the folder is not readable."""
    bd = Path(BENCH_DIR)
    def sub(p, fallback=()):
        try:
            ids = sorted(d.name for d in Path(p).iterdir() if d.is_dir())
            return ids if ids else list(fallback)
        except OSError:
            return list(fallback)
    hard = sub(bd / "hard" / "tasks", HARD_DEFAULT)
    main = sub(bd / "suite" / "code", []) + sub(bd / "suite" / "bugfix", [])
    if (bd / "suite" / "longctx").is_dir():
        main += ["longctx_30k", "longctx_100k"]
    if not main:
        main = list(MAIN_DEFAULT)
    agent = ["agent"] if (bd / "suite" / "agent" / "a1_textstats").is_dir() else []
    return {"hard": hard, "main": main, "agent": agent}

def bench_allowed_tasks():
    return set().union(*bench_catalog().values())

def new_bench_run(label, notes, tasks, samples, max_tokens):
    """Queue a bench run. The settings go ONLY into BENCH_RUNS/<id>/spec.json;
    the job record carries the label for display, never the run settings."""
    jid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
    run = bench_run_dir(jid)          # the generated id always matches the format
    run.mkdir(parents=True)
    (run / "answers").mkdir(exist_ok=True)
    (run / "spec.json").write_text(json.dumps(
        {"id": jid, "label": label, "notes": notes, "tasks": list(tasks),
         "samples": samples, "max_tokens": max_tokens, "url": BENCH_URL}, indent=1),
        encoding="utf-8")
    job_dir(jid).mkdir(parents=True)
    m = {"id": jid, "kind": "bench", "mode": "bench", "folder": "", "job": label,
         "offline": False, "status": "queued", "created": time.time(), "started": None,
         "finished": None, "exit": None, "resume": None, "parent": None, "session": None}
    save_meta(m)
    with queue_cv:
        queue_cv.notify()
    return m

def run_bench(m):
    """Run benchrun.py for one queued bench job. Settings come from the spec in
    BENCH_RUNS only. No watchdog and no sandbox: it is our own script talking to
    the local model. Returns the exit code (0 complete, 143 stopped, 2 server
    down at the start, 3 server stopped answering mid-run, 1 setup problems)."""
    jid = m["id"]
    run = bench_run_dir(jid)
    if not run or not (run / "spec.json").exists():
        m["fail_note"] = "The run folder or its spec.json is missing."
        save_meta(m)
        return 1
    spec = read_json(run / "spec.json") or {}
    url = spec.get("url") or BENCH_URL
    if not basalt_loaded(url):
        m["fail_note"] = "Basalt was not running"
        save_meta(m)
        return 1
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "benchrun.py"), "run", str(run)]
    env = dict(os.environ, BENCH_DIR=BENCH_DIR)
    reap_old_runner(run)   # a board restart can orphan the previous runner; never two writers
    try:
        with open(run / "runner.log", "ab") as log:
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log,
                                 stderr=subprocess.STDOUT, cwd=str(HOME), env=env)
    except OSError as ex:
        m["fail_note"] = f"Could not start benchrun: {ex}"
        save_meta(m)
        return 1
    running[jid] = p
    try:
        (run / "runner.pid").write_text(str(p.pid))
    except OSError:
        pass
    rc = p.wait()
    running.pop(jid, None)
    return rc

def reap_old_runner(run):
    """If a runner from a previous (interrupted) attempt of this run is still
    alive, terminate it before starting a new one, so samples.jsonl never gets
    two writers. Only kills a pid whose /proc cmdline is really benchrun.py."""
    try:
        pid = int((run / "runner.pid").read_text().strip())
    except (OSError, ValueError):
        return
    try:
        if b"benchrun.py" not in Path(f"/proc/{pid}/cmdline").read_bytes():
            return
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass  # already gone (or not ours to signal)

def resume_bench(jid):
    """Re-queue a stopped/interrupted/failed bench run with the SAME id: benchrun
    skips the (task, sample) pairs already in samples.jsonl and continues. A
    failed run (exit 3 = the server stopped answering mid-run) is resumable too,
    which is the whole point of pausing instead of recording a fake 0."""
    m = load_meta(jid)
    if (not m or m.get("kind") != "bench"
            or m["status"] not in ("stopped", "interrupted", "failed")):
        return None
    if check_record(jid, m):   # same guard as the worker: never re-queue a tampered record
        return None
    run = bench_run_dir(jid)
    if not run or not (run / "spec.json").exists():
        return None
    m.update(status="queued", started=None, finished=None, exit=None)
    m.pop("fail_note", None)
    save_meta(m)
    with queue_cv:
        queue_cv.notify()
    return m

def delete_bench_run(jid):
    """Move a bench run's data and job record to .trash/ (never rm)."""
    m = load_meta(jid)
    if not m or m.get("kind") != "bench" or m["status"] in ("queued", "running", "stopping"):
        return False
    moved = False
    run = bench_run_dir(jid)
    if run and run.exists():
        trash = BENCH_RUNS / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(run), str(trash / jid))
        moved = True
    jd = job_dir(jid)
    if jd.exists():
        jtrash = JOBS / ".trash"
        jtrash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(jd), str(jtrash / jid))
    return moved

BENCH_STATE = {"queued": ("QUEUED", "Queued"), "running": ("WORKING", "Running"),
               "stopping": ("WORKING", "Stopping"), "done": ("FINISHED", "Done"),
               "failed": ("FAILED", "Failed"), "interrupted": ("FAILED", "Interrupted"),
               "stopped": ("STOPPED", "Stopped"), "cancelled": ("STOPPED", "Cancelled")}

def bench_overall_text(jid):
    samples = bench_samples(jid)
    if not samples:
        return ""
    try:
        o = benchstats.summarize(samples).get("overall")
    except Exception:
        return ""
    return f"overall {o['mean']:.1f} ± {o['sd']:.1f}" if o else ""

def bench_status(m):
    """jobstatus-shaped dict for the pills/JS, built from progress.json + samples."""
    state, label = BENCH_STATE.get(m["status"], ("STOPPED", m["status"]))
    prog = bench_progress(m["id"]) or {}
    s = {"state": state, "label": label,
         "short": " ".join(str(m.get("job") or m["id"]).split())[:40],
         "elapsed": int((m.get("finished") or time.time()) - m["started"]) if m.get("started") else None,
         "last_activity_ago": None, "actions": 0, "turns": 0, "steps": [], "steps_source": "",
         "fallback": "", "steps_done": prog.get("done", 0), "steps_total": prog.get("total", 0),
         "current_action": prog.get("current") if state == "WORKING" else None,
         "result_summary": "", "fail_reason": m.get("fail_note") or "",
         "pill_extra": f'{prog["done"]}/{prog["total"]} samples' if prog.get("total") else ""}
    if state == "FINISHED":
        s["result_summary"] = bench_overall_text(m["id"])
    if state == "FAILED" and not s["fail_reason"]:
        s["fail_reason"] = f"benchrun exited {m.get('exit')}"
    return s

def bench_live(jid):
    """Live-card data for an active bench run (progress + scores so far)."""
    m = load_meta(jid)
    if not m or m.get("kind") != "bench":
        return None
    prog = bench_progress(jid) or {}
    samples = bench_samples(jid)
    scores = []
    try:
        summ = benchstats.summarize(samples)
        scores = [{"task": t, "mean": summ[t]["mean"], "n": summ[t]["n"]}
                  for t in summ if t != "overall"]
    except Exception:
        pass
    spec = bench_spec(jid) or {}
    st = bench_status(m)
    return {"id": jid, "label": spec.get("label") or m.get("job") or jid, "state": st["state"],
            "done": prog.get("done", 0), "total": prog.get("total", 0),
            "current": prog.get("current"), "eta_s": prog.get("eta_s"), "scores": scores}

def bench_run_rows():
    """One row per bench run, newest first, for the history table."""
    out = []
    for m in all_jobs():
        if m.get("kind") != "bench":
            continue
        jid = m["id"]
        spec = bench_spec(jid) or {}
        env = bench_env(jid) or {}
        prog = bench_progress(jid) or {}
        st = bench_status(m)
        n_tasks = len(spec.get("tasks") or [])
        total = prog.get("total") or n_tasks * int(spec.get("samples") or 0)
        done = prog.get("done", 0)
        summ = None
        if m["status"] in ("done", "stopped", "interrupted", "failed"):
            try:
                summ = benchstats.summarize(bench_samples(jid))
            except Exception:
                summ = None
        o = (summ or {}).get("overall")
        secs = None
        if m.get("started") and m.get("finished"):
            secs = m["finished"] - m["started"]
        out.append({"id": jid, "label": spec.get("label") or m.get("job") or jid,
                    "date": fmt_time(m["created"]), "model": env.get("model") or "-",
                    "ctx": env.get("n_ctx") or "", "samples": f"{done}/{total}",
                    "overall": f"{o['mean']:.1f} ± {o['sd']:.1f}" if o else "-",
                    "duration": benchpages.duration(secs) if secs else "-",
                    "state": st["state"], "state_label": st["label"],
                    "pill_extra": st["pill_extra"] if st["state"] == "WORKING" else "",
                    "can_resume": m["status"] in ("stopped", "interrupted", "failed")
                    and bool(spec)})
    return out

def bench_groups():
    cat = bench_catalog()
    return [
        {"kind": "hard", "title": "Hard tasks (recommended: these separate changes)",
         "hint": "These still fail often enough that a change shows up in the score.",
         "tasks": cat["hard"], "checked": True},
        {"kind": "main", "title": "Main suite (nearly all pass, so it can't tell changes apart)",
         "hint": "Good for spotting a regression, not for measuring an improvement.",
         "tasks": cat["main"], "checked": False},
        {"kind": "agent", "title": "Agent task (slow: 2-10 min per sample)",
         "hint": "The qwen CLI drives it; its seed cannot be set, so samples vary more than the other tasks.",
         "tasks": cat["agent"], "checked": False},
    ]

def bench_page():
    secs = bench_task_secs()
    hard = bench_catalog()["hard"]
    estimate = (len(hard) * 6, sum(secs.get(t, 240) for t in hard) * 6)
    rows = bench_run_rows()
    # The card shows the run that matters: the one RUNNING; only when nothing
    # runs, the OLDEST queued one (the board takes the queue oldest-first, so
    # that is the next to start - a newest-first scan used to show a run that
    # only just joined the queue instead of the one holding the GPU).
    working = [r for r in rows if r["state"] in ("WORKING", "STUCK")]
    queued = [r for r in rows if r["state"] == "QUEUED"]
    pick = working[0] if working else (queued[-1] if queued else None)
    live = bench_live(pick["id"]) if pick else None
    more = max(0, len(working) + len(queued) - (1 if live else 0))
    return render(header() + benchpages.bench_body(bench_groups(), rows, live, secs, estimate,
                                                   more=max(0, more)))

def bench_rows():
    return benchpages.rows_body(bench_run_rows())

def bench_run_page(jid):
    m = load_meta(jid)
    if not m or m.get("kind") != "bench":
        return None
    run = bench_run_dir(jid)
    if not run or not run.exists():
        return None
    spec = bench_spec(jid) or {}
    env = bench_env(jid) or {}
    samples = bench_samples(jid)
    summ = benchstats.summarize(samples) if samples else {}
    st = bench_status(m)
    tasks = [t for t in (spec.get("tasks") or []) if t in summ]
    errors = [{"task": r["task"], "sample": r.get("sample"), "error": r.get("error") or "?",
               "tail": r.get("tail") or ""} for r in samples if r.get("error")]
    log_tail = ""
    if m["status"] in ("failed", "interrupted", "stopped"):
        try:
            log_tail = (run / "runner.log").read_text(errors="replace")[-2000:]
        except OSError:
            pass
    o = summ.get("overall")
    secs = m["finished"] - m["started"] if m.get("started") and m.get("finished") else None
    status_line = " - ".join(x for x in (
        f'{st["steps_done"]}/{st["steps_total"]} samples' if st["steps_total"] else "",
        (f"overall {o['mean']:.1f} ± {o['sd']:.1f}" if o else ""),
        (f"took {benchpages.duration(secs)}" if secs else "")) if x)
    meta_line = " &middot; ".join((
        f"created {fmt_time(m['created'])}",
        f'{len(spec.get("tasks") or [])} tasks x {spec.get("samples", "?")} samples',
        f'max tokens {spec.get("max_tokens", "?")}',
        f"model {env.get('model', '?')} ctx {env.get('n_ctx', '?')}" if env else "no env snapshot yet"))
    data = {"id": jid, "label": spec.get("label") or m.get("job") or jid,
            "notes": spec.get("notes") or "", "meta_line": meta_line,
            "status_line": status_line, "status": st, "env": env, "summary": summ,
            "tasks": tasks, "errors": errors,
            "ignored": (o or {}).get("ignored", 0),
            "length": (o or {}).get("length", 0),
            "fail_note": m.get("fail_note") or "",
            "can_resume": m["status"] in ("stopped", "interrupted", "failed"),
            "live": bench_live(jid) if st["state"] in ("QUEUED", "WORKING") else None,
            "log_tail": log_tail}
    return render(header() + benchpages.run_body(data))

def bench_compare_page(a_id, b_id):
    a_m, b_m = load_meta(a_id), load_meta(b_id)
    if not (a_m and b_m and a_m.get("kind") == "bench" and b_m.get("kind") == "bench"):
        return None
    env_a, env_b = bench_env(a_id), bench_env(b_id)
    cmp = benchstats.compare(bench_samples(a_id), bench_samples(b_id))
    cmp["diffs"] = benchstats.env_diff(env_a, env_b)
    # env.json cannot see the run settings, so a max_tokens 16384 vs 32768 compare
    # would otherwise read as "nothing changed"
    cmp["spec_diffs"] = benchstats.spec_diff(bench_spec(a_id), bench_spec(b_id))
    def side(m):
        spec, env = bench_spec(m["id"]) or {}, bench_env(m["id"]) or {}
        return {"id": m["id"], "label": spec.get("label") or m.get("job") or m["id"],
                "date": fmt_time(m["created"]), "model": env.get("model") or "?",
                "n_ctx": env.get("n_ctx") or "?"}
    return render(header() + benchpages.compare_body(
        {"a": side(a_m), "b": side(b_m), "cmp": cmp, "diffs": cmp["diffs"]}))


# ---------------------------------------------------------------- what a job produced

def events(jid, since=0):
    """Turn the agent's stream-json lines into display items."""
    p = job_dir(jid) / "events.jsonl"
    if not p.exists(): return [], 0
    with open(p, "rb") as f:
        f.seek(since); data = f.read()
    # only consume whole lines
    cut = data.rfind(b"\n") + 1
    data, nxt = data[:cut], since + cut
    items = []
    for raw in data.decode("utf-8", "replace").splitlines():
        raw = raw.strip()
        if not raw: continue
        try: e = json.loads(raw)
        except ValueError:
            if not raw.startswith("Running "): items.append({"k": "log", "t": raw[:500]})
            continue
        t = e.get("type")
        if t == "assistant":
            for c in (e.get("message") or {}).get("content") or []:
                ct = c.get("type")
                if ct == "text" and c.get("text", "").strip(): items.append({"k": "text", "t": c["text"]})
                elif ct == "thinking": items.append({"k": "think"})
                elif ct == "tool_use": items.append({"k": "tool", "name": c.get("name", "?"), "t": summarize(c.get("input") or {})})
        elif t == "user":
            for c in (e.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    if isinstance(body, list): body = "\n".join(x.get("text", "") for x in body if isinstance(x, dict))
                    body = str(body or "")
                    lines = body.splitlines()
                    short = "\n".join(lines[:12]) + (f"\n... ({len(lines) - 12} more lines)" if len(lines) > 12 else "")
                    items.append({"k": "result", "err": bool(c.get("is_error")), "t": short[:2000]})
        elif t == "result":
            items.append({"k": "final", "t": str(e.get("result") or ""), "err": bool(e.get("is_error")),
                          "secs": round((e.get("duration_ms") or 0) / 1000), "turns": e.get("num_turns")})
    return items, nxt

def summarize(inp):
    for key in ("command", "file_path", "path", "pattern", "url", "description", "prompt"):
        if key in inp: return str(inp[key])[:300]
    return json.dumps(inp)[:300]

def in_sandbox(folder, script, timeout=60):
    """Run a read-only command on a project folder inside the sandbox (no network)."""
    try:
        r = subprocess.run(["podman", "run", "--rm", "--userns=keep-id", "--network", "none", "--cap-drop=ALL",
                            "-v", f"{WORK / folder}:/p:ro", "-w", "/p", "--entrypoint", "bash", IMAGE, "-c", script],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as ex:
        return f"(could not read: {ex})"

def git_info(folder):
    return in_sandbox(folder, 'git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "(not a git repository)"; exit; }; '
                              'echo "branch: $(git branch --show-current)"; echo; echo "recent commits:"; git log --oneline -12; '
                              'echo; echo "uncommitted:"; git status --short | head -30')

def safe_path(folder, rel):
    base = (WORK / folder).resolve()
    p = (base / rel).resolve()
    return p if p.is_file() and base in p.parents else None

def build_outputs(folder):
    base = WORK / folder
    found = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.lower().endswith(BUILD_EXT):
                p = Path(root) / f
                if p.is_symlink(): continue
                st = p.stat()
                found.append((st.st_mtime, str(p.relative_to(base)), st.st_size))
        if len(found) > 200: break
    return [{"path": r, "size": s, "mtime": t} for t, r, s in sorted(found, reverse=True)[:30]]

def basalt_status():
    try:
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8080/v1/models", timeout=2))
        return d["data"][0]["status"]["value"]
    except Exception:
        return "offline"

def folders():
    return sorted(d.name for d in WORK.iterdir() if d.is_dir() and not d.name.startswith(".") and d.name != "eval")


# ---------------------------------------------------------------- web

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Jobs</title><style>
:root{--bg:#f6f5f2;--card:#fff;--fg:#1d1d1b;--mut:#6b6a66;--line:#e2e0da;--acc:#2f6fdb;--ok:#2e7d4f;--bad:#c0392b;--warn:#b7791f;--code:#f0efeb}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--fg:#ecebe7;--mut:#a09f9a;--line:#33322f;--acc:#6ea1ff;--ok:#5cc28a;--bad:#ff7b6b;--warn:#e8b04f;--code:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:20px 16px 60px}header{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:22px;margin:0;flex:1}h2{font-size:16px;margin:0 0 10px}a{color:var(--acc)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600;border:1px solid currentColor}
.s-running,.s-stopping{color:var(--acc)}.s-done{color:var(--ok)}.s-failed,.s-interrupted{color:var(--bad)}.s-queued{color:var(--warn)}.s-stopped,.s-cancelled{color:var(--mut)}
.ok{color:var(--ok)}.bad{color:var(--bad)}.mut{color:var(--mut)}
label{display:block;font-weight:600;margin:10px 0 4px}select,input[type=text],input[type=number],textarea{width:100%;padding:9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
textarea{min-height:120px;resize:vertical}.row{display:flex;gap:12px;flex-wrap:wrap}.row>div{flex:1;min-width:220px}
button{background:var(--acc);color:#fff;border:0;border-radius:7px;padding:9px 18px;font:inherit;font-weight:600;cursor:pointer}button.ghost{background:none;color:var(--bad);border:1px solid var(--bad)}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--mut);font-weight:600}
.tbl{overflow-x:auto}.jobtxt{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
pre{background:var(--code);padding:10px;border-radius:7px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;margin:6px 0;font:13px/1.45 ui-monospace,monospace}
.feed .it{padding:6px 0;border-bottom:1px dashed var(--line)}.feed .tool b{color:var(--acc)}.feed .think{color:var(--mut);font-style:italic}
.feed .final{border:2px solid var(--ok);border-radius:8px;padding:10px;margin-top:8px}.feed .final.err{border-color:var(--bad)}
.hint{font-size:13px;color:var(--mut)}
.banner{border-left:5px solid var(--line)}
.banner.WORKING,.banner.FINISHED{border-left-color:var(--ok)}.banner.QUEUED{border-left-color:var(--acc)}
.banner.FAILED{border-left-color:var(--bad)}.banner.STUCK{border-left-color:var(--warn)}.banner.STOPPED{border-left-color:var(--mut)}
.brow{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;background:var(--mut)}
.pill .dot{margin-right:4px;vertical-align:-1px}
.s-WORKING{color:var(--ok)}.s-QUEUED{color:var(--acc)}.s-FINISHED{color:var(--ok)}.s-FAILED{color:var(--bad)}.s-STUCK{color:var(--warn)}.s-STOPPED{color:var(--mut)}
.s-WORKING .dot{background:var(--ok);animation:pulse 1.2s ease-in-out infinite}
.s-QUEUED .dot{background:var(--acc)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.dot{animation:none!important}}
.bstats{margin:10px 0 0;font-size:13px}
.bhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:2px}
.btitle{font-size:26px;font-weight:700;line-height:1.2;display:inline-flex;align-items:center;gap:8px}
.btitle .dot{width:14px;height:14px}
.meta{font-size:13px;margin:12px 0 0}
.bact{font-size:17px;font-weight:600;margin:6px 0 0}
.bwarn{background:rgba(183,121,31,.14);border-radius:7px;padding:8px 10px;margin:8px 0;font-weight:600;color:var(--warn)}
.bsum{margin:6px 0;word-break:break-word}
.prog{margin:10px 0 2px;font-size:14px}
.bar{height:4px;background:var(--line);border-radius:3px;overflow:hidden;margin:2px 0 6px}
.bar>i{display:block;height:100%;background:currentColor;border-radius:3px}
.clamp{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;max-width:340px}
td.now,th.now{white-space:nowrap}td.st,th.st{min-width:220px}
ul.check{list-style:none;margin:4px 0 0;padding:0}
ul.check li{padding:2px 0;display:flex;gap:8px;align-items:baseline}
ul.check li.done{color:var(--mut)}
ul.check li.active{font-weight:600}
button.ghost2{background:none;color:var(--acc);border:1px solid var(--acc);font-weight:500;padding:5px 12px;font-size:13px}
a.btn{display:inline-block;background:var(--acc);color:#fff;border-radius:7px;padding:9px 18px;font-weight:600;text-decoration:none}
.bundle-paste{min-height:160px;font:12px/1.4 ui-monospace,monospace;margin-top:8px}
details summary{cursor:pointer}
.act{max-width:300px}
@media (max-width:640px){.bact{font-size:16px}.act{max-width:none}.btitle{font-size:22px}.clamp{max-width:none}}
</style></head><body><div class="wrap">@@BODY@@</div></body></html>"""

def render(body): return PAGE.replace("@@BODY@@", body)

def esc(s): return html.escape(str(s), quote=True)

def fmt_time(t): return time.strftime("%d %b %H:%M", time.localtime(t)) if t else ""

def dur(m):
    if not m.get("started"): return ""
    s = int((m.get("finished") or time.time()) - m["started"])
    return f"{s // 60}m {s % 60:02d}s"

def header(extra=""):
    st = basalt_status()
    cls = "ok" if st == "loaded" else "bad"
    return (f'<header><h1><a href="/" style="color:inherit;text-decoration:none">Agent jobs</a></h1>'
            f'<a href="/bench">Bench</a> '
            f'<span>Model: <b class="{cls}">{esc(st)}</b></span> <a href="http://127.0.0.1:8080/" target="_blank">GPU stats</a>{extra}</header>')

STATE_GLYPH = {"WORKING": '<span class="dot"></span>', "QUEUED": "&#8230;", "FINISHED": "&#10003;",
               "FAILED": "&#10007;", "STUCK": "&#9888;", "STOPPED": "&#9632;"}


def fmt_ago(secs):
    if secs is None: return ""
    if secs < 60: return f"{secs}s ago"
    if secs < 3600: return f"{secs // 60} min ago"
    return f"{secs // 3600}h {secs % 3600 // 60}m ago"

def fmt_elapsed(secs):
    if secs is None: return ""
    m, s = divmod(secs, 60)
    return f"{m}m {s:02d}s" if m < 60 else f"{m // 60}h {m % 60}m"

def stats_text(s):
    bits = []
    if s.get("elapsed") is not None: bits.append(f"Elapsed {fmt_elapsed(s['elapsed'])}")
    if s["last_activity_ago"] is not None: bits.append(f"last activity {fmt_ago(s['last_activity_ago'])}")
    bits.append(f"{s['actions']} action" + ("s" if s["actions"] != 1 else ""))
    bits.append(f"{s['turns']} turn" + ("s" if s["turns"] != 1 else ""))
    return " &middot; ".join(bits)

def stuck_text(s):
    mins = max(1, round((s["last_activity_ago"] or 0) / 60))
    return f"No new output for {mins} min - it may be stuck. You can Stop it."

def steps_html(s):
    if not s["steps_total"]:
        return f'<p class="mut">{esc(s["fallback"])}</p>' if s.get("fallback") else ""
    sym = {"done": "&#10003;", "active": "&#9679;", "todo": "&#9675;"}
    lis = "".join(f'<li class="{st["state"]}">{sym[st["state"]]} {esc(st["text"])}</li>' for st in s["steps"])
    src = f' <span class="hint">from {esc(s["steps_source"])}</span>' if s["steps_source"] else ""
    pct = round(100 * s["steps_done"] / s["steps_total"])
    return (f'<div class="prog"><b>{s["steps_done"]}/{s["steps_total"]} steps</b>{src}</div>'
            f'<div class="bar s-{s["state"]}"><i style="width:{pct}%"></i></div>'
            f'<ul class="check">{lis}</ul>')

def status_pill(s):
    """Small status badge for the job list; id lets the 3s poller refresh it in place."""
    ex = s.get("pill_extra") or (f'{s["steps_done"]}/{s["steps_total"]}' if s.get("steps_total") else "")
    extra = f' <span class="hint">{esc(ex)}</span>' if ex else ""
    sub = ""
    if s["state"] in ("WORKING", "STUCK") and s.get("current_action"):
        sub = f'<div class="hint act clamp">{esc(s["current_action"])}</div>'
    elif s["state"] == "FINISHED" and s.get("result_summary"):
        sub = f'<div class="hint clamp">{esc(s["result_summary"][:90])}</div>'
    elif s["state"] == "FAILED" and s.get("fail_reason"):
        sub = f'<div class="hint bad clamp">{esc(s["fail_reason"][:90])}</div>'
    return f'<span class="pill s-{s["state"]}">{STATE_GLYPH[s["state"]]} {esc(s["label"])}</span>{extra}{sub}'

def job_rows():
    rows = ""
    for m in all_jobs()[:50]:
        s = status_for(m)
        bench = m.get("kind") == "bench"  # bench rows: folder "-", link to the bench page
        rows += (f'<tr><td class="now"><a href="/{"bench/run" if bench else "job"}/{m["id"]}">{fmt_time(m["created"])}</a></td>'
                 f'<td class="st" id="p-{m["id"]}">{status_pill(s)}</td>'
                 f'<td>{esc(m["mode"])}{" (offline)" if m.get("offline") else ""}</td><td>{esc(m["folder"]) or "-"}</td>'
                 f'<td class="jobtxt" title="{esc(m["job"])}">{"&#8627; " if m.get("parent") else ""}{esc(m["job"])}</td><td>{dur(m)}</td></tr>')
    return rows or '<tr><td colspan=6 class=mut>No jobs yet.</td></tr>'

def home_page():
    opts = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in MODES.items())
    fopts = "".join(f'<option value="{esc(f)}">{esc(f)}</option>' for f in folders())
    rows = job_rows()
    body = header() + f"""
<div class="card"><h2>New job</h2><form method="post" action="/new">
<div class="row"><div><label>Mode</label><select name="mode">{opts}</select></div>
<div><label>Project folder (in ~/agent-work)</label><select name="folder"><option value="">New folder...</option>{fopts}</select>
<input type="text" name="newfolder" placeholder="New folder name, e.g. my-app" style="margin-top:6px"></div></div>
<label>Job</label><textarea name="job" required placeholder="Describe the job the way you would to Claude. Example: Clone https://github.com/... into this folder, add a dark mode toggle to the settings screen, and build a debug APK."></textarea>
{jailcfg.OFFLINE_FIELD}
<p class="hint">Jobs run one at a time. Agents commit to agent/... branches and never push themselves; use Push branch on the finished job. Results land in the project folder with an AGENT_REPORT.md.</p>
<button>Start job</button></form></div>{jailcfg.OFFLINE_JS}
<div class="card"><h2>Jobs</h2><div class="tbl"><table><thead><tr><th class="now">Started</th><th class="st">Status</th><th>Mode</th><th>Folder</th><th>Job</th><th>Time</th></tr></thead><tbody id="jobrows">{rows}</tbody></table></div></div>
<script>
(function(){{
// Refresh only the job list, never the form, so typing is never lost.
// IIFE: classic <script> blocks share ONE global scope - two pages that both
// declare a name at top level (let live / function live) throw a SyntaxError
// that kills the whole second script. Every page script stays inside itself.
const tb=document.getElementById('jobrows');
async function rows(){{try{{const r=await fetch('/api/rows');if(r.ok){{const h=await r.text();if(h!==tb.innerHTML)tb.innerHTML=h;stats()}}}}catch(e){{}}setTimeout(rows,5000)}}
setTimeout(rows,5000);
// Every ~3s refresh the status column and the tab title, without reloading or scrolling.
const GL={{WORKING:'<span class="dot"></span>',QUEUED:'&#8230;',FINISHED:'&#10003;',FAILED:'&#10007;',STUCK:'&#9888;',STOPPED:'&#9632;'}};
const TIT={{WORKING:'\\u25CF',FINISHED:'\\u2713',FAILED:'\\u2717',STUCK:'\\u26A0',QUEUED:'\\u2026',STOPPED:'\\u25CB'}};
const TW={{WORKING:'Working',FINISHED:'Done',FAILED:'Failed',STUCK:'Stuck?',QUEUED:'Queued',STOPPED:'Stopped'}};
function eh(s){{const d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML}}
function pill(s){{const ex=s.pill_extra||(s.steps_total?s.steps_done+'/'+s.steps_total:'');const x=ex?' <span class="hint">'+eh(ex)+'</span>':'';
 let sub='';
 if((s.state==='WORKING'||s.state==='STUCK')&&s.current_action)sub='<div class="hint act clamp">'+eh(s.current_action)+'</div>';
 else if(s.state==='FINISHED'&&s.result_summary)sub='<div class="hint clamp">'+eh(String(s.result_summary).slice(0,90))+'</div>';
 else if(s.state==='FAILED'&&s.fail_reason)sub='<div class="hint bad clamp">'+eh(String(s.fail_reason).slice(0,90))+'</div>';
 return '<span class="pill s-'+s.state+'">'+GL[s.state]+' '+eh(s.label)+'</span>'+x+sub}}
let cur={{}};
function setPills(map){{for(const jid in map){{const el=document.getElementById('p-'+jid);if(el){{el.innerHTML=pill(map[jid]);el.dataset.st=map[jid].state;
 const b=el.firstElementChild;if(b)b.className='pill s-'+map[jid].state}}}}cur=map}}
async function stats(){{try{{const r=await fetch('/api/statuses');if(r.ok){{const d=await r.json();setPills(d.statuses);
 let live=null;for(const jid in d.statuses){{const s=d.statuses[jid];if(!live&&(s.state==='WORKING'||s.state==='STUCK'))live=s}}
 if(live){{const p=live.pill_extra?' '+live.pill_extra:(live.steps_total?' '+live.steps_done+'/'+live.steps_total:'');document.title=TIT[live.state]+p+' '+(live.state==='STUCK'?'Stuck?':TW[live.state])+' \\u2013 '+live.short}}
}}}}catch(e){{}}setTimeout(stats,3000)}}
stats();
// Keep an unsent draft in this browser, so even a reload or closed tab doesn't lose it.
const f=document.querySelector('form[action="/new"]'),keys=['mode','folder','newfolder','job'];
try{{const d=JSON.parse(localStorage.getItem('jobdraft')||'{{}}');keys.forEach(k=>{{if(d[k]!==undefined&&f.elements[k])f.elements[k].value=d[k]}});if(d.offline)f.elements.offline.checked=true}}catch(e){{}}
function save(){{try{{const d={{}};keys.forEach(k=>d[k]=f.elements[k].value);d.offline=f.elements.offline.checked;localStorage.setItem('jobdraft',JSON.stringify(d))}}catch(e){{}}}}
f.addEventListener('input',save);f.addEventListener('change',save);
f.addEventListener('submit',()=>{{try{{localStorage.removeItem('jobdraft')}}catch(e){{}}}});
}})();
</script>"""
    return render(body)

def job_page(jid):
    m = load_meta(jid)
    if not m: return None
    report_p = safe_path(m["folder"], "AGENT_REPORT.md")
    report = report_p.read_text(errors="replace")[:60000] if report_p else ""
    outs = build_outputs(m["folder"])
    out_html = "".join(f'<li><a href="/download/{jid}?path={urllib.parse.quote(o["path"])}">{esc(o["path"])}</a> '
                       f'<span class="mut">{o["size"] / 1e6:.1f} MB, {fmt_time(o["mtime"])}</span></li>' for o in outs)
    active = m["status"] in ("queued", "running", "stopping")
    if not active and not m.get("session") and not READONLY:
        sid = session_of(jid)
        if sid: m["session"] = sid; save_meta(m)
    stop = f'<form method="post" action="/stop/{jid}" style="display:inline"><button class="ghost">{"Cancel" if m["status"] == "queued" else "Stop"}</button></form>' if active else ""
    follow_html = ""
    if not active and m.get("session"):
        follow_html = (f'<div class="card"><h2>Follow up</h2><form method="post" action="/followup/{jid}">'
                       f'<textarea name="job" required placeholder="Ask a follow-up question or give the next instruction. It continues this job\'s conversation, in the same folder."></textarea>'
                       f'<p class="hint">Runs as a new job in the queue, carrying on from where this one finished.</p><button>Send</button></form></div>')
    s = status_for(m)
    glyph = STATE_GLYPH[s["state"]]
    summary = s["fail_reason"] if s["state"] == "FAILED" else (s["result_summary"] or "")
    jt = m["job"] or ""
    if len(jt) > 300 or jt.count("\n") > 5:  # a long prompt must not push the status below the fold
        job_html = f'<details><summary class="hint">Show the job text</summary><pre>{esc(jt)}</pre></details>'
    else:
        job_html = f'<pre>{esc(jt)}</pre>'
    meta_bits = [jailcfg.badge_html(m) + esc(MODES.get(m["mode"], m["mode"]))]   # JAIL: sandbox badge
    if m.get("offline"): meta_bits.append("offline")
    meta_bits.append(f'folder ~/agent-work/{esc(m["folder"])}')
    meta_bits.append(f"created {fmt_time(m['created'])}")
    if m.get("parent") and load_meta(m["parent"]):
        meta_bits.append(f'follow-up to <a href="/job/{m["parent"]}">earlier job</a>')
    meta_line = " &middot; ".join(meta_bits)
    bundle_html = ""
    if m["status"] in ("done", "failed", "stopped", "interrupted"):
        bundle_html = f"""<div class="card"><h2>Review bundle</h2>
<div class="brow" id="bundlebox"><button id="copybundle">Copy for Claude</button>
<a class="btn" href="/api/bundle/{esc(jid)}?download=1">Download .md</a></div>
<p class="hint">One file with the prompt, the agent's summary, build/test results and the changed lines - paste it into Claude to review.</p></div>
"""
    body = header() + f"""
<div class="card banner {s['state']}" id="banner">
<div class="bhead"><span class="btitle s-{s['state']}" id="st">{glyph} {esc(s['label'])}</span><span style="flex:1"></span><span id="stopform">{stop}</span></div>
<div class="bact" id="bact"{' style="display:none"' if not s['current_action'] else ''}>{esc(s['current_action'] or '')}</div>
<div class="bwarn" id="bstuck"{' hidden' if s['state'] != 'STUCK' else ''}>{esc(stuck_text(s))}</div>
<div class="bsum{' bad' if s['state'] == 'FAILED' else ''}" id="bsum">{esc(summary)}</div>
<div class="bstats mut" id="bstats">{stats_text(s)}</div>
<div id="bsteps">{steps_html(s)}</div>
<p class="mut meta">{meta_line}</p>
<div id="bfoot"><button class="ghost2" id="alertbtn" hidden>Enable alerts</button></div></div>
{bundle_html}{jailcfg.push_card_html(m, jid, in_sandbox, READONLY)}{jailcfg.BADGE_CSS}<div class="card"><h2>Job</h2>{job_html}</div>
{follow_html}<div class="card"><h2>Activity</h2><details id="det"><summary class="hint">Show details</summary><div class="feed" id="feed"><span class="mut">Waiting for the agent...</span></div></details></div>
<div class="card"><h2>Report</h2>{f'<pre>{esc(report)}</pre>' if report else '<span class="mut">No AGENT_REPORT.md yet.</span>'}</div>
<div class="card"><h2>Changes</h2><pre id="git" class="mut">Loading...</pre></div>
<div class="card"><h2>Build outputs</h2>{f'<ul>{out_html}</ul>' if outs else '<span class="mut">None found yet (APK, AAB, EXE, ZIP...).</span>'}</div>
<script>
(function(){{
// IIFE so this script can never collide with another <script> on the page.
const jid={json.dumps(jid)},jobShort={json.dumps(" ".join((m['job'] or 'job').split())[:40])};let off=0,first=true,prev=null;
const feed=document.getElementById('feed'),det=document.getElementById('det'),banner=document.getElementById('banner');
function el(t,c,h){{const d=document.createElement(t);if(c)d.className=c;if(h!==undefined)d.textContent=h;return d}}
function eh(s){{const d=document.createElement('div');d.textContent=(s==null?'':String(s));return d.innerHTML}}
function add(it){{if(first){{feed.textContent='';first=false}}let d=el('div','it '+it.k);
 if(it.k==='think'){{if(feed.lastChild&&feed.lastChild.classList.contains('think'))return;d.textContent='thinking...'}}
 else if(it.k==='tool'){{const b=el('b',null,it.name+' ');d.appendChild(b);d.appendChild(el('code',null,it.t))}}
 else if(it.k==='result'){{const p=el('pre',it.err?'bad':'mut',it.t);d.appendChild(p)}}
 else if(it.k==='final'){{d.className='it final'+(it.err?' err':'');d.appendChild(el('b',null,(it.err?'Finished with an error':'Finished')+(it.secs?' in '+Math.round(it.secs/60)+' min':'')));d.appendChild(el('pre',null,it.t))}}
 else d.appendChild(el(it.k==='log'?'pre':'div',it.k==='log'?'mut':null,it.t));
 feed.appendChild(d)}}
async function poll(){{try{{const r=await fetch('/api/events/'+jid+'?since='+off);const j=await r.json();
 const atEnd=det.open&&innerHeight+scrollY>=document.body.offsetHeight-80;
 j.items.forEach(add);off=j.next;
 if(j.items.length&&atEnd&&!first)scrollTo(0,document.body.scrollHeight);
}}catch(e){{}}setTimeout(poll,2000)}}
poll();
// The status banner, checklist and tab title refresh every ~3s: no reload, no scroll jump.
const GL={{WORKING:'<span class="dot"></span>',QUEUED:'&#8230;',FINISHED:'&#10003;',FAILED:'&#10007;',STUCK:'&#9888;',STOPPED:'&#9632;'}};
const TIT={{WORKING:'\\u25CF',FINISHED:'\\u2713',FAILED:'\\u2717',STUCK:'\\u26A0',QUEUED:'\\u2026',STOPPED:'\\u25CB'}};
const TW={{WORKING:'Working',FINISHED:'Done',FAILED:'Failed',STUCK:'Stuck?',QUEUED:'Queued',STOPPED:'Stopped'}};
function fmtDur(s){{if(s==null)return '';const m=Math.floor(s/60),x=s%60;return m<60?m+'m '+String(x).padStart(2,'0')+'s':Math.floor(m/60)+'h '+(m%60)+'m'}}
function fmtAgo(s){{if(s==null)return '';if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+' min ago';return Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m ago'}}
function stepsH(j){{if(!j.steps_total)return j.fallback?'<p class="mut">'+eh(j.fallback)+'</p>':'';
 const sym={{done:'\\u2713',active:'\\u25CF',todo:'\\u25CB'}};
 const w=Math.round(100*j.steps_done/j.steps_total);
 let h='<div class="prog"><b>'+j.steps_done+'/'+j.steps_total+' steps</b>'+(j.steps_source?' <span class="hint">from '+eh(j.steps_source)+'</span>':'')+'</div><div class="bar s-'+j.state+'"><i style="width:'+w+'%"></i></div><ul class="check">';
 for(const t of j.steps)h+='<li class="'+t.state+'">'+(sym[t.state]||'\\u25CB')+' '+eh(t.text)+'</li>';
 return h+'</ul>'}}
function beep(ok){{try{{const C=window.AudioContext||window.webkitAudioContext,c=new C(),o=c.createOscillator(),g=c.createGain();o.connect(g);g.connect(c.destination);o.type='sine';o.frequency.value=ok?880:240;g.gain.value=.15;o.start();const t=c.currentTime;g.gain.exponentialRampToValueAtTime(.001,t+.4);o.stop(t+.45);setTimeout(()=>c.close(),700)}}catch(e){{}}}}
function apply(j){{banner.className='card banner '+j.state;
 const stp=document.getElementById('st');stp.className='btitle s-'+j.state;stp.innerHTML=GL[j.state]+' '+eh(j.label);
 const stats=[];if(j.elapsed!=null)stats.push('Elapsed '+fmtDur(j.elapsed));
 if(j.last_activity_ago!=null)stats.push('last activity '+fmtAgo(j.last_activity_ago));
 stats.push(j.actions+(j.actions===1?' action':' actions'));stats.push(j.turns+(j.turns===1?' turn':' turns'));
 document.getElementById('bstats').innerHTML=stats.join(' &middot; ');
 const act=document.getElementById('bact');act.textContent=j.current_action||'';act.style.display=j.current_action?'':'none';
 const stk=document.getElementById('bstuck');
 if(j.state==='STUCK'){{stk.hidden=false;stk.textContent='No new output for '+Math.max(1,Math.round((j.last_activity_ago||0)/60))+' min - it may be stuck. You can Stop it.'}}else stk.hidden=true;
 const sum=document.getElementById('bsum');sum.className='bsum'+(j.state==='FAILED'?' bad':'');
 sum.textContent=j.state==='FAILED'?(j.fail_reason||''):j.state==='FINISHED'?(j.result_summary||''):'';
 document.getElementById('bsteps').innerHTML=stepsH(j);
 const prog=j.steps_total?' '+j.steps_done+'/'+j.steps_total:'';
 document.title=TIT[j.state]+prog+' '+(j.state==='STUCK'?'Stuck?':TW[j.state])+' \\u2013 '+jobShort;
 const live=['QUEUED','WORKING','STUCK'].includes(j.state);
 if(!live){{const sf=document.getElementById('stopform');if(sf)sf.remove();
  if(prev&&['QUEUED','WORKING','STUCK'].includes(prev)&&!document.getElementById('rl')){{const a=document.createElement('a');a.id='rl';a.className='hint';a.href='';a.textContent=' Reload the page to see the report and follow up.';document.getElementById('bfoot').appendChild(a)}}}}
 if(prev&&['QUEUED','WORKING','STUCK'].includes(prev)&&(j.state==='FINISHED'||j.state==='FAILED')){{beep(j.state==='FINISHED');
  if('Notification'in window&&Notification.permission==='granted'){{try{{new Notification(j.state==='FINISHED'?'Agent job finished':'Agent job failed',{{body:jobShort}})}}catch(e){{}}}}
  setTimeout(()=>location.reload(),4000);}}  // bring in the Report/Changes/Follow up cards; the Reload link stays as fallback
 prev=j.state}}
async function pollStatus(){{try{{const r=await fetch('/api/status/'+jid);if(r.ok)apply(await r.json())}}catch(e){{}}setTimeout(pollStatus,3000)}}
pollStatus();
// Browser alerts: permission is asked only when Jason presses the button, never automatically.
const btn=document.getElementById('alertbtn');
if('Notification'in window){{if(Notification.permission==='granted'){{btn.hidden=false;btn.textContent='Alerts on';btn.disabled=true}}
 else{{btn.hidden=false;btn.onclick=()=>{{try{{Notification.requestPermission().then(p=>{{btn.hidden=p!=='granted';btn.textContent=p==='granted'?'Alerts on':'Enable alerts';btn.disabled=p==='granted'}})}}catch(e){{}}}}}}}}
fetch('/api/git/'+jid).then(r=>r.text()).then(t=>{{const g=document.getElementById('git');g.textContent=t;g.className=''}});
// One click packs the whole job into one Markdown file to paste into Claude for a code review.
const cb=document.getElementById('copybundle');
if(cb)cb.onclick=()=>{{cb.disabled=true;cb.textContent='Building bundle...';
 const txt=fetch('/api/bundle/'+jid).then(r=>{{if(!r.ok)throw new Error('bundle failed');return r.text()}});
 // Firefox: after an await the click's user activation may be gone, so when
 // ClipboardItem exists the clipboard write starts synchronously, right here,
 // with a promise for the body. Otherwise fall back to writeText, then textarea.
 let wres=null;
 if(window.ClipboardItem){{try{{wres=navigator.clipboard.write([new ClipboardItem({{'text/plain':txt.then(t=>new Blob([t],{{type:'text/plain'}}))}})]);}}catch(e){{wres=null;}}}}
 const reset=ms=>setTimeout(()=>{{cb.disabled=false;if(!cb.textContent.startsWith('Clipboard'))cb.textContent='Copy for Claude';}},ms);
 const show=t=>{{cb.textContent='Copied - '+Math.round(t.length/4)+' tokens';reset(3000)}};
 const manual=t=>{{const ta=document.createElement('textarea');ta.value=t;ta.className='bundle-paste';
  document.getElementById('bundlebox').appendChild(ta);ta.focus();ta.select();
  cb.textContent='Clipboard refused - copy the text below with Ctrl+C';reset(6000);}};
 const byText=t=>{{if(navigator.clipboard&&navigator.clipboard.writeText)
   navigator.clipboard.writeText(t).then(()=>show(t),()=>manual(t));else manual(t);}};
 const got=t=>{{if(wres&&wres.then)wres.then(()=>show(t),()=>byText(t));else byText(t);}};
 txt.then(got).catch(()=>{{cb.textContent='Could not build the bundle';reset(3000);}});}};
}})();
</script>"""
    return render(body)


class H(http.server.BaseHTTPRequestHandler):
    server_version = "jobboard"

    def log_message(self, *a): pass

    def guard(self, post=False):
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            self.send_error(403, "Bad host"); return False
        q = urllib.parse.urlparse(self.path).query
        given = urllib.parse.parse_qs(q).get("t", [None])[0]
        cookie = re.search(r"(?:^|;\s*)jb=([^;]+)", self.headers.get("Cookie", ""))
        if given and secrets.compare_digest(given, TOKEN):
            self.send_response(303); self.send_header("Set-Cookie", f"jb={TOKEN}; HttpOnly; SameSite=Strict; Path=/")
            self.send_header("Location", "/"); self.end_headers(); return False
        if not (cookie and secrets.compare_digest(cookie.group(1), TOKEN)):
            self.send_error(403, "Open the link printed when the job board started"); return False
        if post:
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
                self.send_error(403, "Bad origin"); return False
        return True

    def send(self, body, ctype="text/html; charset=utf-8", code=200, headers=()):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store"); self.send_header("X-Frame-Options", "DENY")
        for k, v in headers: self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)

    def redirect(self, to):
        self.send_response(303); self.send_header("Location", to); self.end_headers()

    def do_GET(self):
        if self.path == "/favicon.ico":  # browsers request this before login: no cookie needed
            self.send_response(204); self.end_headers(); return
        if not self.guard(): return
        u = urllib.parse.urlparse(self.path); parts = u.path.strip("/").split("/")
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/": return self.send(home_page())
        if u.path == "/api/rows": return self.send(job_rows())
        if u.path == "/bench": return self.send(bench_page())
        if u.path == "/api/bench/rows": return self.send(bench_rows())
        if parts == ["bench", "compare"]:
            page = bench_compare_page((q.get("a") or [""])[0], (q.get("b") or [""])[0])
            return self.send(page) if page else self.send_error(404)
        if len(parts) == 3 and parts[:2] == ["bench", "run"]:
            page = bench_run_page(parts[2])
            return self.send(page) if page else self.send_error(404)
        if len(parts) == 4 and parts[:3] == ["api", "bench", "live"]:
            j = bench_live(parts[3])
            return self.send(json.dumps(j), "application/json") if j else self.send_error(404)
        if len(parts) == 2 and parts[0] == "job" and load_meta(parts[1]):
            jm = load_meta(parts[1])
            if jm.get("kind") == "bench": return self.redirect(f"/bench/run/{jm['id']}")
            return self.send(job_page(parts[1]))
        if u.path == "/api/statuses":
            return self.send(json.dumps({"statuses": {m["id"]: status_for(m) for m in all_jobs()[:50]}}), "application/json")
        if len(parts) == 3 and parts[:2] == ["api", "status"] and load_meta(parts[2]):
            return self.send(json.dumps(status_for(load_meta(parts[2]))), "application/json")
        if len(parts) == 3 and parts[:2] == ["api", "events"] and load_meta(parts[2]):
            m = load_meta(parts[2]); items, nxt = events(parts[2], int(q.get("since", ["0"])[0] or 0))
            return self.send(json.dumps({"items": items, "next": nxt, "status": m["status"], "dur": dur(m)}), "application/json")
        if len(parts) == 3 and parts[:2] == ["api", "git"] and load_meta(parts[2]):
            return self.send(git_info(load_meta(parts[2])["folder"]), "text/plain; charset=utf-8")
        if len(parts) == 3 and parts[:2] == ["api", "bundle"] and load_meta(parts[2]):
            m = load_meta(parts[2])
            if m.get("kind") == "bench":
                return self.send("Bench runs have no review bundle.", "text/plain; charset=utf-8", code=404)
            if m["status"] in ("queued", "running", "stopping"):
                return self.send("The job has not finished yet - the review bundle comes after it does.",
                                 "text/plain; charset=utf-8", code=409)
            md = saved_review_md(m)   # the worker builds this when the job ends
            if md is None:
                try:
                    md = reviewbundle.build_bundle(m, job_dir(m["id"]) / "events.jsonl",
                                                   lambda s: in_sandbox(m["folder"], s, timeout=120), load_meta)
                except Exception as ex:
                    return self.send(f"(bundle failed: {ex})", "text/plain; charset=utf-8", code=500)
            hdrs = []
            if (q.get("download") or [""])[0] == "1":
                safe_id = re.sub(r"[^A-Za-z0-9._-]", "", m["id"])
                hdrs.append(("Content-Disposition", f'attachment; filename="review-{safe_id}.md"'))
            if not READONLY:  # read-only copies never write into the job records
                try:
                    (job_dir(m["id"]) / "review.md").write_text(md)
                except OSError:
                    pass
            return self.send(md, "text/markdown; charset=utf-8", headers=hdrs)
        if len(parts) == 2 and parts[0] == "download" and load_meta(parts[1]):
            p = safe_path(load_meta(parts[1])["folder"], q.get("path", [""])[0])
            if not p: return self.send_error(404)
            self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{p.name}"'); self.send_header("Content-Length", str(p.stat().st_size))
            self.end_headers()
            with open(p, "rb") as f: shutil.copyfileobj(f, self.wfile)
            return
        self.send_error(404)

    def do_POST(self):
        if not self.guard(post=True): return
        if READONLY:
            return self.send_error(403, "This job board runs read-only: starting or stopping jobs is disabled")
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        n = int(self.headers.get("Content-Length", "0") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(min(n, 200000)).decode("utf-8", "replace"))
        get = lambda k: (form.get(k, [""])[0]).strip()
        if parts == ["new"]:
            mode, job = get("mode"), get("job")
            folder = get("newfolder") or get("folder")
            if mode not in MODES or not job or not FOLDER_RE.match(folder or "") or folder == "eval":
                return self.send(render(header() + '<div class="card bad">Pick a mode, a valid folder name (letters, numbers, dot, dash, underscore) and describe the job. <a href="/">Back</a></div>'), code=400)
            m = new_job(mode, folder, job, bool(get("offline")) and mode != "supervised")
            return self.redirect(f"/job/{m['id']}")
        if len(parts) == 2 and parts[0] == "followup" and load_meta(parts[1]):
            prev = load_meta(parts[1]); job = get("job")
            if not job or not prev.get("session") or prev["status"] in ("queued", "running", "stopping"):
                return self.redirect(f"/job/{parts[1]}")
            if check_record(parts[1], prev):   # never build a follow-up on a tampered record
                return self.redirect(f"/job/{parts[1]}")
            m = new_job(prev["mode"], prev["folder"], job, prev.get("offline", False), resume=prev["session"], parent=prev["id"])
            return self.redirect(f"/job/{m['id']}")
        if len(parts) == 2 and parts[0] == "push" and load_meta(parts[1]):   # JAIL: Push branch button
            pm = load_meta(parts[1])
            if (pm.get("kind") != "bench" and pm["status"] in ("done", "failed", "stopped", "interrupted")
                    and not check_record(parts[1], pm)):
                jailcfg.push_branch(pm, WORK / pm["folder"], get("branch"))
                save_meta(pm, parts[1])
            return self.redirect(f"/job/{parts[1]}")
        if len(parts) == 2 and parts[0] == "stop" and load_meta(parts[1]):
            was_bench = load_meta(parts[1]).get("kind") == "bench"
            threading.Thread(target=stop_job, args=(parts[1],), daemon=True).start()
            time.sleep(0.5)
            return self.redirect(f"/bench/run/{parts[1]}" if was_bench else f"/job/{parts[1]}")
        if parts == ["bench", "new"]:
            label = get("label")[:80]
            tasks = [t for t in form.get("tasks", []) if t]
            allowed = bench_allowed_tasks()
            try:
                nsamples, ntok = int(get("samples")), int(get("max_tokens"))
            except ValueError:
                nsamples = ntok = 0
            if (not label or not tasks or any(t not in allowed for t in tasks)
                    or not 1 <= nsamples <= 12 or not 1024 <= ntok <= 131072):
                return self.send(render(header() + '<div class="card bad">Pick a label, tasks from the list, '
                                        'samples 1-12 and max tokens 1024-131072. <a href="/bench">Back</a></div>'), code=400)
            m = new_bench_run(label, get("notes")[:4000], tasks, nsamples, ntok)
            return self.redirect(f"/bench/run/{m['id']}")
        if len(parts) == 3 and parts[:2] == ["bench", "resume"]:
            resume_bench(parts[2]); return self.redirect(f"/bench/run/{parts[2]}")
        if len(parts) == 3 and parts[:2] == ["bench", "delete"]:
            delete_bench_run(parts[2]); return self.redirect("/bench")
        self.send_error(404)


def _has_records(path):
    """True if a jobs folder holds any job records (.trash doesn't count)."""
    try:
        return any(d.is_dir() and d.name != ".trash" for d in path.iterdir())
    except OSError:
        return False

def old_jobs_notice():
    """The records moved from WORK/.jobs to JOBS (outside the sandbox). We never
    move, read or run anything from the old place; if it still holds records
    while the new one is empty, say so once, in one clear line, at startup."""
    old = WORK / ".jobs"
    if _has_records(old) and not _has_records(JOBS):
        print(f"NOTE: job records are still in the old place: {old}. "
              f"This board reads {JOBS}; move the records over by hand.", flush=True)
        return True
    return False

def main():
    if "--test-notify" in sys.argv:  # fire one desktop notification and exit
        notify_finished({"id": "test", "job": "Test notification from the job board"}, True)
        print("Sent one success notification (errors are ignored; check the desktop and sound).", flush=True)
        return
    old_jobs_notice()
    if READONLY:
        print("Read-only mode: no worker thread, jobs are never changed.", flush=True)
    else:
        threading.Thread(target=worker, daemon=True).start()
    srv = http.server.ThreadingHTTPServer((HOST, PORT), H)
    print(f"Job board running. Open: http://127.0.0.1:{PORT}/?t={TOKEN}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
