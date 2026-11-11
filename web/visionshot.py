#!/usr/bin/env python3
"""Upgrade 6a (# U06): the screenshot driver that runs INSIDE the job sandbox.

The board pipes this program into the sandbox with a here-doc (the sandbox mounts only
the job's project folder, so it can never read a file from the tool), hands it the check
spec in the VISION_SPEC_B64 environment variable and reads the PNGs back as one base64
JSON line on stdout - so nothing is ever written inside the project folder.

Spec:  {"open": "index.html" or "http://127.0.0.1:<port>/path", "size": [w, h],
        "steps": [{"click": sel} | {"type": sel, "text": t} | {"fill": sel, "text": t}
                  | {"key": k} | {"wait": ms} | {"wait_for": sel} | {"shot": name}],
        "step_timeout_ms": 10000}
Local files are served by a temporary http server on 127.0.0.1 (module pages need
http://, not file://). <port> in an http URL is replaced by that server's port.

Answer (last stdout line): VISIONSHOT_JSON <base64 of
  {"shots": [{"name": "final", "b64": "..."}], "url": "...", "steps": [...], "error": null}
"""
import base64
import functools
import http.server
import json
import os
import re
import sys
import threading
import urllib.parse

MARK = "VISIONSHOT_JSON"


def emit(out):
    sys.stdout.write("\n%s %s\n" % (MARK, base64.b64encode(json.dumps(out).encode()).decode()))
    sys.stdout.flush()


def log(msg):
    sys.stderr.write("[visionshot] %s\n" % msg)
    sys.stderr.flush()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(folder):
    """A throw-away static server for the project folder; returns (server, port)."""
    handler = functools.partial(_Quiet, directory=folder)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def target_url(open_spec, folder, srv_port):
    """The URL to open: a file in the project goes through the local server."""
    open_spec = (open_spec or "index.html").strip()
    if re.match(r"^https?://", open_spec):
        if srv_port is not None:
            open_spec = open_spec.replace("<port>", str(srv_port))
        return open_spec
    rel = open_spec.lstrip("/")
    return "http://127.0.0.1:%d/%s" % (srv_port, urllib.parse.quote(rel))


def do_step(page, step, tout):
    if "click" in step:
        page.click(step["click"], timeout=tout)
    elif "type" in step or "fill" in step:
        page.fill(step.get("type") or step.get("fill"), str(step.get("text", "")), timeout=tout)
    elif "key" in step:
        page.keyboard.press(str(step["key"]))
    elif "wait" in step:
        page.wait_for_timeout(max(0, int(step["wait"])))
    elif "wait_for" in step:
        page.wait_for_selector(step["wait_for"], timeout=tout)
    else:
        return "unknown step"
    return None


def main():
    out = {"shots": [], "steps": [], "url": "", "error": None}
    try:
        spec = json.loads(base64.b64decode(os.environ.get("VISION_SPEC_B64", "")))
    except Exception as ex:
        out["error"] = "the check spec could not be read (%s)" % ex
        emit(out)
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        out["error"] = ("Playwright for Python is not installed in the sandbox image "
                        "(pip install playwright + playwright install chromium)")
        out["no_browser"] = True
        emit(out)
        return 0

    w, h = (spec.get("size") or [1280, 800])[:2]
    tout = int(spec.get("step_timeout_ms") or 10000)
    folder = os.getcwd()
    srv = None
    rc = 0
    try:
        if not re.match(r"^https?://", (spec.get("open") or "").strip() or "index.html"):
            srv = serve(folder)
        url = target_url(spec.get("open"), folder, srv.server_address[1] if srv else None)
        out["url"] = url
        shots = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": int(w), "height": int(h)})
            try:
                page.goto(url, wait_until="load", timeout=30000)
                page.wait_for_timeout(400)
            except Exception as ex:
                out["error"] = "the page did not open: %s" % ex
            for i, step in enumerate(spec.get("steps") or []):
                if isinstance(step, dict) and "shot" in step:
                    shots.append((str(step.get("shot") or ("step%d" % i)), page.screenshot(type="png")))
                    continue
                err = do_step(page, step, tout) if isinstance(step, dict) else "not a step"
                out["steps"].append({"step": i, "what": json.dumps(step)[:200], "error": err})
                if err:
                    log("step %d: %s" % (i, err))
            shots.append(("final", page.screenshot(type="png")))
            browser.close()
        for name, png in shots:
            out["shots"].append({"name": name, "b64": base64.b64encode(png).decode()})
    except Exception as ex:
        out["error"] = "the screenshot failed: %s" % ex
        rc = 1
    finally:
        if srv is not None:
            srv.shutdown()
    emit(out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
