#!/usr/bin/env python3
"""Resolve against signed snapshot metadata; fetch exact .debs with SHA-256 checks.

Try Debian's normal pool first (same version/hash), then the recorded snapshot
URI. This avoids slow snapshot backends for bytes still present on the CDN.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shlex
import subprocess
import time
import urllib.parse
import urllib.request


def main():
    packages = Path("/opt/worker-packages.txt").read_text().split()
    Path("/tmp/empty-status").touch()
    output = Path("/opt/worker-debs")
    output.mkdir(parents=True, exist_ok=True)
    plan = subprocess.check_output(["apt-get", "-o", "Dir::State::status=/tmp/empty-status", "--print-uris",
                                    "--yes", "--download-only", "--no-install-recommends", "install", *packages], text=True)
    downloads = []
    for line in plan.splitlines():
        if not line.startswith("'https://"):
            continue
        uri, filename, size, _ = shlex.split(line)
        name = filename.split("_", 1)[0]
        metadata = subprocess.check_output(["apt-cache", "show", name], text=True)
        selected = None
        for paragraph in metadata.split("\n\n"):
            fields = dict(l.split(": ", 1) for l in paragraph.splitlines() if l and not l.startswith(" ") and ": " in l)
            if fields.get("Filename") and urllib.parse.unquote(uri).endswith("/" + fields["Filename"]):
                selected = fields
                break
        if not selected or len(selected.get("SHA256", "")) != 64:
            raise SystemExit("Missing SHA-256 from verified snapshot metadata for " + filename)
        downloads.append((uri, filename, int(size), selected["Filename"], selected["SHA256"]))

    def download(item):
        uri, filename, size, pool_path, expected = item
        destination = output / filename
        mirrors = ["https://deb.debian.org/debian/" + pool_path, uri]
        for attempt in range(6):
            url = mirrors[attempt % len(mirrors)]
            try:
                with urllib.request.urlopen(url, timeout=45) as source:
                    content = source.read()
                if len(content) != size or hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError("Debian package checksum mismatch")
                destination.write_bytes(content)
                destination.chmod(0o644)
                return
            except (OSError, ValueError):
                if attempt == 5:
                    raise
                time.sleep(1)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(download, downloads))
    print(f"Verified {len(downloads)} complete worker dependency packages from pinned signed snapshot metadata")


if __name__ == "__main__":
    main()
