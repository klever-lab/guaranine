#!/usr/bin/env python3
"""Run on the existing coordinator; emit only a protected per-worker input file."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def capture(*args, data=None):
    return subprocess.check_output(args, input=data, stderr=subprocess.DEVNULL).decode().strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("node", help="Unique worker identity shown by first-boot setup")
    parser.add_argument("output", type=Path, help="New mode-0600 output outside the checkout")
    args = parser.parse_args()
    if os.geteuid() != 0 or not re.fullmatch(r"worker-[0-9a-f]{20}", args.node):
        parser.error("Root on the coordinator and the exact worker identity are required")
    target = args.output.resolve()
    if target.is_relative_to(ROOT) or target.exists():
        parser.error("Output must be a new file outside the checkout")
    os.umask(0o077)
    namespace = "enroll-" + args.node[7:]
    manifest = (ROOT / "provision/worker/verification-rbac.yaml").read_text().replace("VERIFY_NAMESPACE", namespace).replace("NODE_NAME", args.node)
    capture("k3s", "kubectl", "apply", "-f", "-", data=manifest.encode())
    ca = Path("/var/lib/rancher/k3s/server/tls/server-ca.crt").read_text()
    bootstrap = capture("k3s", "token", "create", "--ttl", "1h", "--description", args.node)
    if not bootstrap.startswith("K10"):
        bootstrap = "K10" + hashlib.sha256(ca.encode()).hexdigest() + "::" + bootstrap
    verification = capture("k3s", "kubectl", "-n", namespace, "create", "token", "enrollment-verifier", "--duration=1h")
    address = capture("tailscale", "ip", "-4")
    config = {"server": f"https://{address}:6443", "server_ca": ca, "bootstrap_token": bootstrap,
              "verification_namespace": namespace, "verification_token": verification}
    with target.open("x") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    target.chmod(0o600)
    print("Per-worker credentials written to the protected output; expire after one hour.")


if __name__ == "__main__":
    main()
