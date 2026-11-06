#!/usr/bin/env python3
"""Remaster pinned upstream d-i media, preserving its hybrid BIOS/UEFI boot images."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
INPUTS = json.loads((ROOT / "images/laptop/inputs.json").read_text())
PUBLIC_FILES = ("worker.py", "guaranine-worker", "tailscaled.service", "guaranine-setup.service",
                "guaranine-enroll.service", "guaranine-enroll.timer", "guaranine-network.service", "network.nft", "sshd.conf", "debian.sources")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(spec, destination):
    """A failed/mismatched download is never used, including from the optional cache."""
    destination = Path(destination)
    cache = Path(os.environ.get("GUARANINE_DOWNLOAD_CACHE", tempfile.gettempdir())) / spec["sha256"]
    if not cache.exists() or digest(cache) != spec["sha256"]:
        for attempt in range(5):
            try:
                urls = [spec["url"], *spec.get("fallback_urls", [])]
                url = urls[attempt % len(urls)]
                with urllib.request.urlopen(url, timeout=180) as source, cache.with_suffix(".part").open("wb") as target:
                    shutil.copyfileobj(source, target)
                if digest(cache.with_suffix(".part")) != spec["sha256"]:
                    raise ValueError("download checksum mismatch")
                cache.with_suffix(".part").replace(cache)
                break
            except (OSError, ValueError):
                cache.with_suffix(".part").unlink(missing_ok=True)
                if attempt == 4:
                    raise
                time.sleep(2 ** attempt)
    shutil.copyfile(cache, destination)


def run(*args):
    subprocess.run(args, check=True)


def build(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    iso = output / "guaranine-worker-amd64.iso"
    manifest = output / "build.json"
    if iso.exists() or manifest.exists():
        raise SystemExit("Use an empty output directory; versioned assets must never be overwritten")
    with tempfile.TemporaryDirectory(prefix="guaranine-build-") as scratch:
        stage = Path(scratch)
        base = stage / "upstream.iso"
        fetch(INPUTS["debian"], base)
        payload = stage / "guaranine"
        packages = payload / "packages"
        packages.mkdir(parents=True)
        bundle = Path(os.environ.get("GUARANINE_PACKAGE_BUNDLE", "/opt/worker-debs"))
        if not list(bundle.glob("*.deb")):
            raise SystemExit("Build with images/laptop/build.sh; the pinned complete APT package bundle is required")
        for path in sorted(bundle.glob("*.deb")):
            shutil.copyfile(path, packages / path.name)
        (packages / "SHA256SUMS").write_text("".join(f"{digest(p)}  {p.name}\n" for p in sorted(packages.glob("*.deb"))))
        worker = payload / "worker"
        # Deliberate allowlist: never copy .git, operator files, host state or caches.
        worker.mkdir(parents=True)
        for relative in ("install.sh", "playbook.yml", "ansible.cfg", "inventory.ini"):
            shutil.copyfile(ROOT / "provision/worker" / relative, worker / relative)
        (worker / "files").mkdir()
        for filename in PUBLIC_FILES:
            shutil.copyfile(ROOT / "provision/worker/files" / filename, worker / "files" / filename)
        shutil.copyfile(ROOT / "images/laptop/inputs.json", worker / "inputs.json")
        shutil.copyfile(ROOT / "images/laptop/packages.txt", worker / "apt-packages.txt")
        binaries = worker / "vendor"
        binaries.mkdir()
        for name, filename in (("k3s", "k3s"), ("k3s_install", "install-k3s.sh"), ("tailscale", "tailscale.tgz")):
            fetch(INPUTS[name], binaries / filename)
        with tarfile.open(binaries / "tailscale.tgz") as archive:
            for name in ("tailscale", "tailscaled"):
                member = archive.getmember(f'tailscale_{INPUTS["tailscale"]["version"]}_amd64/{name}')
                with archive.extractfile(member) as source, (binaries / name).open("wb") as target:
                    shutil.copyfileobj(source, target)
        (binaries / "tailscale.tgz").unlink()
        for filename in ("late.sh", "select-disk.sh", "disk.templates"):
            shutil.copyfile(ROOT / "images/laptop" / filename, payload / filename)
        for path in payload.rglob("*"):
            if path.is_file():
                path.chmod(0o644)
        source_commit = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip() if shutil.which("git") else os.environ["SOURCE_COMMIT"]
        dirty = bool(subprocess.check_output(["git", "-C", str(ROOT), "status", "--porcelain"], text=True).strip()) if shutil.which("git") else os.environ.get("SOURCE_DIRTY", "true") != "false"
        data = {"schema": 1, "source_commit": source_commit, "source_dirty": dirty,
                "inputs": INPUTS, "payload_sha256": {str(p.relative_to(payload)): digest(p) for p in sorted(payload.rglob("*")) if p.is_file()},
                "builder_packages": subprocess.check_output(["dpkg-query", "-W", "-f=${Package}=${Version}\n"], text=True).splitlines(),
                "qemu_tests": "pending", "reproducibility": "See images/laptop/README.md; not bit-for-bit reproducible"}
        data["installer_config_sha256"] = {name: digest(ROOT / "images/laptop" / name) for name in
                                           ("preseed.cfg", "grub.cfg", "isolinux.cfg", "Dockerfile", "build.py", "packages.txt", "download-packages.py")}
        data["bundled_packages"] = [subprocess.check_output(["dpkg-deb", "--show", "--showformat=${Package}=${Version} (${Architecture})", str(p)], text=True)
                                    for p in sorted(packages.glob("*.deb"))]
        (payload / "build.json").write_text(json.dumps(data, indent=2) + "\n")
        preseed = ROOT / "images/laptop/preseed.cfg"
        # Preserve Debian's on-media integrity checker after remastering. Upstream
        # excludes mutable boot catalog/isolinux boot-info bytes from this list.
        checksums = stage / "md5sum.txt"
        run("xorriso", "-osirrox", "on", "-indev", str(base), "-extract", "/md5sum.txt", str(checksums))
        checksums.chmod(0o644)
        replaced = {"./boot/grub/grub.cfg", "./isolinux/isolinux.cfg", "./preseed.cfg"}
        entries = [line for line in checksums.read_text().splitlines() if line.split()[-1] not in replaced]
        mapped = [(p, "./guaranine/" + str(p.relative_to(payload))) for p in payload.rglob("*") if p.is_file()]
        mapped += [(preseed, "./preseed.cfg"), (ROOT / "images/laptop/isolinux.cfg", "./isolinux/isolinux.cfg"),
                   (ROOT / "images/laptop/grub.cfg", "./boot/grub/grub.cfg")]
        for source, target in mapped:
            with source.open("rb") as stream:
                entries.append(hashlib.file_digest(stream, "md5").hexdigest() + "  " + target)
        checksums.write_text("\n".join(sorted(entries)) + "\n")
        run("xorriso", "-indev", str(base), "-outdev", str(iso), "-boot_image", "any", "replay",
            "-volid", "GUARANINE", "-map", str(payload), "/guaranine",
            "-map", str(preseed), "/preseed.cfg",
            "-map", str(ROOT / "images/laptop/isolinux.cfg"), "/isolinux/isolinux.cfg",
            "-map", str(ROOT / "images/laptop/grub.cfg"), "/boot/grub/grub.cfg",
            "-map", str(checksums), "/md5sum.txt",
            "-commit", "-end")
        data["iso_sha256"] = digest(iso)
        manifest.write_text(json.dumps(data, indent=2) + "\n")
        (output / "SHA256SUMS").write_text(f'{digest(iso)}  {iso.name}\n{digest(manifest)}  {manifest.name}\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    build(parser.parse_args().output)
