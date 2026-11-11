#!/bin/bash
# JAIL: entry.sh - entrypoint for local-job containers (--network none).
# Starts the Basalt bridge (127.0.0.1:8080 -> /run/basalt/basalt.sock), waits until
# it answers, then hands over to the image's normal entrypoint (seeds home, runs CMD).
python3 /opt/jail/bridge.py 2>/dev/null &
for _ in $(seq 100); do
  (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null && break
  sleep 0.05
done
exec /opt/agent/bin/entrypoint "$@"
