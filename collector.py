#!/usr/bin/env python3
"""
wg-stats collector
==================
Samples one or more WireGuard / AmneziaWG interfaces on a node, computes
per-peer throughput, and writes a JSON snapshot the wg-stats viewer consumes.
Optionally pushes that snapshot to the viewer host over rsync + ssh.

Runs as a long-lived daemon (see wg-stats-collector.service) so it can derive
rate-up / rate-down from consecutive samples.

Snapshot schema (what index.html expects):
{
  "hostname": "<full hostname>",
  "generated_at": <unix seconds>,
  "interfaces": {
    "<iface>": { "peers": [
      { "public_key", "endpoint"|null, "allowed_ips",
        "rx_bytes", "tx_bytes", "rx_speed", "tx_speed",
        "handshake_age"|null, "online" }
    ] }
  }
}
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time

# ─────────────────────────────── CONFIG ───────────────────────────────
# Interfaces to sample, each as (iface_name, command_prefix). The collector
# runs:  <command_prefix> show <iface_name> dump
#   native AmneziaWG : ["awg"]
#   native WireGuard : ["wg"]
#   docker (wg-easy) : ["docker", "exec", "wg-easy", "wg"]
INTERFACES = [
    ("wg0",  ["awg"]),
    ("awg0", ["awg"]),
    # ("wg0", ["docker", "exec", "wg-easy", "wg"]),
]

# Full hostname reported in the snapshot. Change to static if needed.
HOSTNAME = socket.gethostname()

# Short node name — used only for the output filename (stats-<NODE>.json).
NODE = "YOUR_NODE_NAME_HERE"

# Seconds between samples. Small enough for meaningful rates.
INTERVAL = 2

# A peer counts as "online" if its last handshake is newer than this (seconds).
ONLINE_MAX = 180

# Where to write the snapshot locally before pushing.
OUT_DIR = "/run/wg-stats"

# Push target on the viewer host. Leave "" to only write locally
# (e.g. when the collector runs ON the viewer host).
#   format: user@YOUR_IP_HERE:/path/served/by/nginx/
RSYNC_DEST = "deploy@YOUR_IP_HERE:/opt/wg-stats/"

# SSH private key and port used for the rsync push. Use key name from ls ~/.ssh/
SSH_KEY = "/root/.ssh/YOUR_KEY_NAME_HERE"
SSH_PORT = "YOUR_PORT_HERE"
# ───────────────────────────────────────────────────────────────────────


def dump(iface, cmd):
    """Return `<cmd> show <iface> dump` output, or "" on failure."""
    try:
        return subprocess.run(
            cmd + ["show", iface, "dump"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


def parse(out):
    """Parse a `dump` into raw peer dicts (counters only — rates added later)."""
    peers = []
    for line in out.strip().splitlines()[1:]:      # line 0 is the interface itself
        f = line.split("\t")
        if len(f) < 8:
            continue
        pub, _psk, endpoint, allowed, hs, rx, tx, _keep = f[:8]
        peers.append({
            "public_key": pub,
            "endpoint": None if endpoint in ("(none)", "") else endpoint,
            "allowed_ips": allowed,
            "rx_bytes": int(rx),
            "tx_bytes": int(tx),
            "_hs": int(hs),                         # unix ts, 0 = never
        })
    return peers


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"stats-{NODE}.json")
    prev = {}                                       # (iface, pubkey) -> (rx, tx)
    prev_mono = time.monotonic()

    while True:
        now_wall = int(time.time())
        now_mono = time.monotonic()
        dt = max(now_mono - prev_mono, 1e-3)

        interfaces, seen = {}, {}
        for iface, cmd in INTERFACES:
            peers = parse(dump(iface, cmd))
            for p in peers:
                hs = p.pop("_hs")
                age = (now_wall - hs) if hs > 0 else None
                p["handshake_age"] = age
                p["online"] = age is not None and age <= ONLINE_MAX

                key = (iface, p["public_key"])
                last = prev.get(key)
                p["rx_speed"] = max(0, p["rx_bytes"] - last[0]) / dt if last else 0
                p["tx_speed"] = max(0, p["tx_bytes"] - last[1]) / dt if last else 0
                seen[key] = (p["rx_bytes"], p["tx_bytes"])
            interfaces[iface] = {"peers": peers}

        prev, prev_mono = seen, now_mono

        snapshot = {
            "hostname": HOSTNAME,
            "generated_at": now_wall,
            "interfaces": interfaces,
        }

        # atomic local write
        fd, tmp = tempfile.mkstemp(dir=OUT_DIR, suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            json.dump(snapshot, fh, separators=(",", ":"))
        os.replace(tmp, out_path)

        # push to the viewer host (--chmod=F644 so nginx can read it -> no 403)
        if RSYNC_DEST:
            subprocess.run(
                ["rsync", "-az", "--chmod=F644",
                 "-e", f"ssh -i {SSH_KEY} -p {SSH_PORT} -o StrictHostKeyChecking=accept-new",
                 out_path, RSYNC_DEST],
                check=False,
            )

        time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)