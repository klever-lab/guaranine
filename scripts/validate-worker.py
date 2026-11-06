#!/usr/bin/env python3
"""Lint the runnable worker/installer/workflows alongside behavioral unit tests."""
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("iso_build", ROOT / "images/laptop/build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)
ACTIONLINT = {
    "linux_amd64": "023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757",
    "linux_arm64": "401942f9c24ed71e4fe71b76c7d638f66d8633575c4016efd2977ce7c28317d0",
    "darwin_amd64": "28e5de5a05fc558474f638323d736d822fff183d2d492f0aecb2b73cc44584f5",
    "darwin_arm64": "2693315b9093aeacb4ebd91a993fea54fc215057bf0da2659056b4bc033873db",
}


def main():
    shell = sorted([*ROOT.glob("images/laptop/*.sh"), *ROOT.glob("scripts/iso/*.sh"), *ROOT.glob("provision/worker/*.sh"), ROOT / "provision/worker/files/guaranine-worker"])
    subprocess.run(["shellcheck", "--external-sources", "--exclude=SC1091", *map(str, shell)], check=True)
    for path in shell:
        subprocess.run(["bash", "-n", str(path)], check=True)
    with tempfile.TemporaryDirectory(prefix="guaranine-lint-") as directory:
        environment = {**os.environ, "ANSIBLE_LOCAL_TEMP": directory, "ANSIBLE_REMOTE_TEMP": directory}
        subprocess.run(["ansible-playbook", "--syntax-check", "--inventory", "localhost,", str(ROOT / "provision/worker/playbook.yml")], env=environment, check=True)
        system = platform.system().lower()
        architecture = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        target = f"{system}_{architecture}"
        if target not in ACTIONLINT:
            raise SystemExit("Actionlint validation supports Linux/macOS AMD64/ARM64")
        archive = Path(directory) / "actionlint.tar.gz"
        build.fetch({"url": f"https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_{target}.tar.gz", "sha256": ACTIONLINT[target]}, archive)
        with tarfile.open(archive) as package:
            executable = Path(directory) / "actionlint"
            executable.write_bytes(package.extractfile("actionlint").read())
            executable.chmod(0o755)
        subprocess.run([str(executable), "-color"], cwd=ROOT, check=True)
    # Python compiles into temporary storage: no runtime/cache data in the ISO.
    for path in [*ROOT.glob("images/laptop/*.py"), *ROOT.glob("scripts/iso/*.py"), *ROOT.glob("provision/worker/files/*.py")]:
        compile(path.read_text(), str(path), "exec")
    json.loads((ROOT / "images/laptop/inputs.json").read_text())


if __name__ == "__main__":
    main()
