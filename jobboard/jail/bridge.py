#!/usr/bin/env python3
# JAIL: bridge.py - runs INSIDE a local-job container that has --network none.
# Listens on 127.0.0.1:8080 (the only address the agent CLIs know for Basalt) and
# forwards each connection to the unix socket /run/basalt/basalt.sock, which the
# host side (podman-local.sh, socat) connects to the real Basalt. Nothing else is
# reachable: the container has no network interface except loopback.
# Written by Claude, 2026-11-11. Standard library only (the image has python3, not socat).
import os, socket, sys, threading

SOCK = os.environ.get("JAIL_BASALT_SOCK", "/run/basalt/basalt.sock")
PORT = int(os.environ.get("JAIL_BASALT_PORT", "8080"))

def pump(src, dst):
    try:
        while True:
            b = src.recv(65536)
            if not b:
                break
            dst.sendall(b)
    except OSError:
        pass
    finally:
        try: dst.shutdown(socket.SHUT_WR)
        except OSError: pass

def handle(c):
    u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        u.connect(SOCK)
    except OSError:
        c.close(); u.close(); return
    t = threading.Thread(target=pump, args=(u, c), daemon=True); t.start()
    pump(c, u)
    t.join()
    c.close(); u.close()

def main():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", PORT))
    s.listen(64)
    while True:
        c, _ = s.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()

if __name__ == "__main__":
    main()
