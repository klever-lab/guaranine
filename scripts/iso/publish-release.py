#!/usr/bin/env python3
"""Release-job-only publisher. Never overwrite assets on a versioned release."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

SPEC = importlib.util.spec_from_file_location("finalize", Path(__file__).with_name("finalize.py"))
finalize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finalize)


def gh(*args, check=True):
    return subprocess.run(["gh", *args], stdout=subprocess.PIPE, check=check, text=True)


def publish(directory):
    directory = Path(directory)
    manifest, tests = finalize.validate(directory, require_clean=True)
    tag = os.environ["RELEASE_TAG"]
    assert re.fullmatch(r"worker-iso-v[0-9]+\.[0-9]+\.[0-9]+", tag)
    assert manifest["source_commit"] == os.environ["EXPECTED_COMMIT"]
    assert manifest["qemu_tests"] == tests
    subprocess.run(["sha256sum", "--check", "SHA256SUMS"], cwd=directory, check=True)
    assets = ["guaranine-worker-amd64.iso", "build.json", "qemu-results.json", "SHA256SUMS"]
    existing = gh("release", "view", tag, "--json", "assets,isDraft", check=False)
    with tempfile.TemporaryDirectory(prefix="guaranine-release-") as temporary:
        temporary = Path(temporary)
        if existing.returncode:
            notes = temporary / "notes.md"
            notes.write_text(f"Debian amd64 laptop installer from `{manifest['source_commit']}`.\n\n"
                             "BIOS and UEFI installation, real K3s/Tailscale enrollment on disposable Headscale identities, "
                             "scheduler placement, rejected credentials, coordinator outage and reboot checks passed. "
                             "Hosted Tailscale and physical laptop firmware/WAN behavior require the operator checks in images/laptop/README.md.\n\n"
                             "Verify SHA256SUMS before writing the ISO to USB. See build.json for pins and installed packages.\n")
            gh("release", "create", tag, "--draft", "--verify-tag", "--target", manifest["source_commit"], "--title", tag, "--notes-file", str(notes))
            published = set()
        else:
            info = json.loads(existing.stdout)
            published = {a["name"] for a in info["assets"]}
            assert published <= set(assets), "Unexpected versioned assets; refusing to mutate this release"
            for name in sorted(published):
                gh("release", "download", tag, "--pattern", name, "--dir", str(temporary))
                assert finalize.digest(temporary / name) == finalize.digest(directory / name), "Versioned asset differs; use the original artifact or a new version tag"
        for name in assets:
            if name not in published:
                gh("release", "upload", tag, str(directory / name))
        # Publication is last: failed uploads leave a recoverable draft.
        gh("release", "edit", tag, "--draft=false")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    publish(parser.parse_args().directory)
