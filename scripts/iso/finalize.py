#!/usr/bin/env python3
"""Refuse incomplete, stale, skipped, or foreign-image QEMU evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import re

REQUIRED = ("installation", "explicit_disk_confirmation", "automatic_enrollment", "rejected_credentials",
            "coordinator_outage_retry", "reboot_identity_and_no_duplicates", "reboot_after_bootstrap_and_verifier_revocation",
            "credential_replacement_while_retrying", "resource_requests_scheduled", "embedded_derp")


def package_records(output):
    """Remove terminal framing, preserving every actual dpkg package/version row."""
    if isinstance(output, list):
        output = "\n".join(output)
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    rows = [line.strip() for line in output.splitlines() if line.strip()]
    if not rows or any(not re.fullmatch(r"[a-z0-9][a-z0-9+.:_-]*=[A-Za-z0-9.+:~_-]+", row) for row in rows):
        raise ValueError("Invalid or missing installed package/version records")
    return rows


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate(directory, require_clean=False):
    directory = Path(directory)
    iso = directory / "guaranine-worker-amd64.iso"
    manifest = json.loads((directory / "build.json").read_text())
    tests = json.loads((directory / "qemu-results.json").read_text())
    assert manifest["iso_sha256"] == tests["iso_sha256"] == digest(iso), "QEMU evidence belongs to another ISO"
    assert manifest["source_commit"] == tests["source_commit"], "QEMU evidence belongs to another source commit"
    assert tests["unique_identities"] == "passed", "Two independent installations are required"
    assert set(tests["firmware"]) == {"bios", "uefi"}
    for result in tests["firmware"].values():
        assert all(result[key] == "passed" for key in REQUIRED), "A required image test did not pass"
        assert result["installed_packages"], "Missing actual installed package manifest"
        assert result["installed_packages"] == package_records(result["installed_packages"]), "Package manifest contains terminal framing"
    if require_clean:
        assert manifest["source_dirty"] is False, "Releases require a clean reviewed source tree"
    return manifest, tests


def finalize(directory):
    directory = Path(directory)
    manifest, tests = validate(directory)
    manifest["qemu_tests"] = tests
    (directory / "build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (directory / "SHA256SUMS").write_text("".join(f"{digest(directory / name)}  {name}\n" for name in
        ("guaranine-worker-amd64.iso", "build.json", "qemu-results.json")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    finalize(parser.parse_args().directory)
