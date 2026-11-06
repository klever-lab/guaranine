"""Select the pinned Terraform CLI, downloading a verified temporary copy if needed."""

import hashlib
import io
import json
from pathlib import Path
import platform
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile


CHECKSUMS_PATH = Path(__file__).with_name("terraform-checksums.json")


def ensure_terraform(version, directory, env):
    checksums = json.loads(CHECKSUMS_PATH.read_text())
    if checksums["version"] != version:
        raise SystemExit("Update scripts/terraform-checksums.json to match .terraform-version")

    installed = shutil.which("terraform", path=env.get("PATH"))
    if installed:
        result = subprocess.run(
            [installed, "version", "-json"], env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False,
        )
        try:
            installed_version = json.loads(result.stdout).get("terraform_version")
        except json.JSONDecodeError:
            installed_version = None
        if result.returncode == 0 and installed_version == version:
            return Path(installed)

    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    architecture = {
        "x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
    }.get(platform.machine().lower())
    target = f"{system}_{architecture}"
    if target not in checksums["sha256"]:
        raise SystemExit("Terraform bootstrap supports Linux/macOS on AMD64/ARM64; use Linux/WSL or install the pinned CLI")

    print(f"Downloading checksum-pinned Terraform {version} for {target} into temporary storage", flush=True)
    filename = f"terraform_{version}_{target}.zip"
    url = f"https://releases.hashicorp.com/terraform/{version}/{filename}"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            archive = response.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise SystemExit(f"Cannot download pinned Terraform from {url}: {error}") from error
    if hashlib.sha256(archive).hexdigest() != checksums["sha256"][target]:
        raise SystemExit(f"Terraform SHA-256 mismatch for {filename}; refusing to install")

    binary = directory / "terraform"
    # Read only the expected member, never arbitrary paths from the archive.
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        binary.write_bytes(zipped.read("terraform"))
    binary.chmod(0o755)
    return binary
