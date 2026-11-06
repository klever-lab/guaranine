#!/usr/bin/env python3
"""Validate both roles with real pinned talosctl and lint the runnable tooling."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/talos'))
from common import PINS, fetch, talosctl

ACTIONLINT = {
    'linux_amd64': '023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757',
    'linux_arm64': '401942f9c24ed71e4fe71b76c7d638f66d8633575c4016efd2977ce7c28317d0',
    'darwin_amd64': '28e5de5a05fc558474f638323d736d822fff183d2d492f0aecb2b73cc44584f5',
    'darwin_arm64': '2693315b9093aeacb4ebd91a993fea54fc215057bf0da2659056b4bc033873db',
}


def main():
    os.umask(0o077)
    binary = talosctl()
    environment = {**os.environ, 'GUARANINE_TALOSCTL_BINARY': binary}
    schematic = yaml.safe_load((ROOT / 'talos/schematic.yaml').read_text())
    if schematic != {'customization': {'systemExtensions': {'officialExtensions': ['siderolabs/tailscale']}}}:
        raise ValueError('Public schematics may contain only the declared extension; never machine configuration or credentials')
    with tempfile.TemporaryDirectory(prefix='guaranine-talos-validation-') as scratch:
        scratch = Path(scratch)
        cli = [sys.executable, str(ROOT / 'scripts/talos/config.py')]
        subprocess.run([*cli, 'init', '--output', str(scratch / 'cluster')], env=environment, check=True)
        for role in ('controlplane', 'worker'):
            now = datetime.now(timezone.utc)
            key = scratch / (role + '-key.json')
            key.write_text(json.dumps({'id': 'disposable-fixture', 'key': 'tskey-auth-DISPOSABLE-NOT-A-REAL-KEY',
                                      'created': now.isoformat(), 'expires': (now + timedelta(hours=24)).isoformat(),
                                      'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': False,
                                                     'preauthorized': True, 'tags': ['tag:guaranine-' + role]}}}}))
            key.chmod(0o600)
            args = [*cli, 'node', '--name', 'test-' + role, '--role', role, '--endpoint', 'https://100.64.0.1:6443',
                    '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda',
                    '--secrets', str(scratch / 'cluster/secrets.yaml'), '--tailscale-key', str(key),
                    '--output', str(scratch / role)]
            if role == 'controlplane':
                args += ['--etcd-disk-selector', 'disk.dev_path == "/dev/vdb"']
            subprocess.run(args, env=environment, check=True)
            docs = list(yaml.safe_load_all((scratch / role / 'machine.yaml').read_text()))
            machine = next(doc for doc in docs if doc.get('version') == 'v1alpha1')
            assert machine['machine']['type'] == role
            assert not any(doc.get('kind') == 'KubePrismConfig' for doc in docs)
            assert next(doc for doc in docs if doc.get('kind') == 'KubeNodeConfig')['nodeIP']['validSubnets'] == ['100.64.0.0/10']
            if role == 'controlplane':
                assert next(doc for doc in docs if doc.get('kind') == 'KubeFlannelCNIConfig')['extraArgs'] == ['--iface=tailscale0']
            if role == 'worker':
                assert not machine['machine']['ca'].get('key')
                assert not machine.get('cluster', {}).get('etcd')
        target = platform.system().lower() + '_' + {'x86_64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower(), '')
        if target not in ACTIONLINT:
            raise ValueError('Unsupported validation platform')
        archive = scratch / 'actionlint.tar.gz'
        fetch({'url': f'https://github.com/rhysd/actionlint/releases/download/v1.7.7/actionlint_1.7.7_{target}.tar.gz', 'sha256': ACTIONLINT[target]}, archive)
        with tarfile.open(archive) as package:
            executable = scratch / 'actionlint'
            executable.write_bytes(package.extractfile('actionlint').read())
            executable.chmod(0o755)
        subprocess.run([str(executable)], cwd=ROOT, check=True)
    shell = [ROOT / 'images/laptop/build.sh', ROOT / 'scripts/talos/check-release-ref.sh', ROOT / 'scripts/validate.sh']
    subprocess.run(['shellcheck', *map(str, shell)], check=True)
    for path in ROOT.glob('scripts/talos/*.py'):
        compile(path.read_text(), str(path), 'exec')
    print('Talos control-plane/worker schemas, Tailscale-only profiles, workflows and shell scripts validated')


if __name__ == '__main__':
    main()
