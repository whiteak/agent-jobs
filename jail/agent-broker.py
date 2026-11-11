#!/usr/bin/env python3
"""JAIL: agent-broker.py - host-side helper for SUPERVISED jobs (2026-11-11, Claude).

The supervised container (internet + Claude login) has no way to run a local agent
itself: its local-claude / local-qwen are shims (jail/shim/local-agent) that send a
request to this broker over a private unix socket. The broker:

  * accepts ONLY {mode in local-claude/local-qwen, prompt, output_format, verbose,
    resume id | continue, cwd}, or {mode: sandbox-run, command}: a build/test command
    that runs in the same OFFLINE sandbox (so agent-written build scripts never run in
    the supervisor's container, which holds the Claude login)
  * requires cwd to be the job's own folder (/work/FOLDER or below)
  * runs the self-test (jail/podman-selftest.sh) before the first local run
  * runs the agent through jail/run-local.sh = the same offline container as normal
    local jobs (no network, state/home-local, only this folder), one at a time
  * streams stdout/stderr back, then the exit code
  * on SIGTERM (supervised job ended or stopped) stops every child container

Started and stopped by ~/agent-sandbox/agent for each supervised run:
  agent-broker.py --sock DIR/broker.sock --work ~/agent-work --folder FOLDER --name CONTAINER
"""
import argparse, json, os, re, signal, socket, struct, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.realpath(__file__))
RUN_LOCAL = os.path.join(HERE, "run-local.sh")
SELFTEST = os.path.join(HERE, "podman-selftest.sh")
MODES = ("local-claude", "local-qwen")
SESSION_RE = re.compile(r"[A-Za-z0-9-]{8,80}")
FORMATS = ("text", "json", "stream-json")
MAX_REQ = 1_000_000

ap = argparse.ArgumentParser()
ap.add_argument("--sock", required=True)
ap.add_argument("--work", required=True)
ap.add_argument("--folder", required=True)
ap.add_argument("--name", required=True)       # supervisor container name (children: NAME-local-N)
ap.add_argument("--log", default="")
A = ap.parse_args()
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", A.folder):
    sys.exit("broker: bad folder")
HOSTDIR = os.path.join(A.work, A.folder)

run_lock = threading.Lock()                     # one local agent at a time (one GPU stream)
children = {}                                   # container name -> Popen
selftest_ok = False
counter = 0
logf = open(A.log, "a", buffering=1) if A.log else None


def log(msg):
    if logf:
        logf.write(time.strftime("%H:%M:%S ") + msg + "\n")


def frame(conn, kind, data):
    conn.sendall(kind + struct.pack(">I", len(data)) + data)


def check_cwd(req):
    cwd = req.get("cwd") or ""
    base = f"/work/{A.folder}"
    if not isinstance(cwd, str) or os.path.normpath(cwd) != base and not os.path.normpath(cwd).startswith(base + "/"):
        raise ValueError(f"run it from the project folder {base} (it was {cwd})")


def build_args(req):
    mode = req.get("mode")
    if mode == "sandbox-run":   # a build/test command, run in the same OFFLINE sandbox (no model)
        cmd = req.get("command")
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > 20_000:
            raise ValueError("command missing or too long")
        check_cwd(req)
        return mode, ["-c", cmd]
    if mode not in MODES:
        raise ValueError("mode must be local-claude or local-qwen")
    prompt = req.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 200_000:
        raise ValueError("prompt missing or too long")
    check_cwd(req)
    args = ["-p", prompt]
    fmt = req.get("output_format")
    if fmt:
        if fmt not in FORMATS:
            raise ValueError("output format must be text, json or stream-json")
        args += (["-o", fmt] if mode == "local-qwen" else ["--output-format", fmt])
    if req.get("verbose") is True and mode == "local-claude":
        args.append("--verbose")
    if req.get("resume"):
        if not isinstance(req["resume"], str) or not SESSION_RE.fullmatch(req["resume"]):
            raise ValueError("bad resume id")
        args += ["--resume", req["resume"]]
    elif req.get("continue") is True:
        args.append("--continue")
    return mode, args


def handle(conn):
    global selftest_ok, counter
    try:
        raw = b""
        while not raw.endswith(b"\n"):
            b = conn.recv(65536)
            if not b or len(raw) > MAX_REQ:
                return
            raw += b
        try:
            mode, args = build_args(json.loads(raw))
        except (ValueError, json.JSONDecodeError) as ex:
            frame(conn, b"E", f"refused: {ex}\n".encode()); frame(conn, b"X", b"2"); return
        if not run_lock.acquire(blocking=False):
            frame(conn, b"E", b"(another local agent is running - waiting for it to finish)\n")
            run_lock.acquire()
        try:
            if not selftest_ok:
                r = subprocess.run([SELFTEST], capture_output=True, text=True, timeout=180)
                log("selftest rc=%d" % r.returncode)
                if r.returncode != 0:
                    frame(conn, b"E", ("Sandbox check failed, local agent NOT started:\n" + r.stdout).encode())
                    frame(conn, b"X", b"3"); return
                selftest_ok = True
            counter += 1
            cname = f"{A.name}-local-{counter}"
            log(f"start {cname} {mode} {len(args[1])} chars")
            p = subprocess.Popen([RUN_LOCAL, mode, HOSTDIR, A.folder, cname, "--"] + args,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            children[cname] = p

            def pump_err():
                for chunk in iter(lambda: p.stderr.read1(65536), b""):
                    try: frame(conn, b"E", chunk)
                    except OSError: break
            t = threading.Thread(target=pump_err, daemon=True); t.start()
            for chunk in iter(lambda: p.stdout.read1(65536), b""):
                try: frame(conn, b"O", chunk)
                except OSError:            # the supervisor gave up on this call: stop the child
                    subprocess.run(["podman", "stop", "-t", "5", cname], capture_output=True)
                    break
            rc = p.wait(); t.join(timeout=5)
            children.pop(cname, None)
            log(f"end {cname} rc={rc}")
            try: frame(conn, b"X", str(rc).encode())
            except OSError: pass
        finally:
            run_lock.release()
    finally:
        conn.close()


def shutdown(*_):
    for cname, p in list(children.items()):
        subprocess.run(["podman", "stop", "-t", "5", cname], capture_output=True, timeout=60)
        try: p.terminate()
        except Exception: pass
    log("broker stopped")
    try: os.unlink(A.sock)
    except OSError: pass
    os._exit(0)


signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)
srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
old = os.umask(0o177); srv.bind(A.sock); os.umask(old)
srv.listen(8)
log(f"broker up folder={A.folder} name={A.name}")
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
