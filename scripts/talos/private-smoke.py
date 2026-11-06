#!/usr/bin/env python3
"""Build both private formats with disposable credentials; never upload the results."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from common import ROOT, talosctl


def main():
    os.umask(0o077)
    environment = {**os.environ, 'GUARANINE_TALOSCTL_BINARY': talosctl()}
    config = [sys.executable, str(ROOT / 'scripts/talos/config.py')]
    images = [sys.executable, str(ROOT / 'scripts/talos/images.py')]
    with tempfile.TemporaryDirectory(prefix='guaranine-disposable-private-images-') as scratch:
        scratch = Path(scratch)
        subprocess.run([*config, 'init', '--output', str(scratch/'cluster')], env=environment, check=True)
        for role, image_format in (('worker', 'iso'), ('controlplane', 'azure')):
            now = datetime.now(timezone.utc)
            key = scratch / (role + '.json')
            key.write_text(json.dumps({'id': 'disposable-ci', 'key': 'tskey-auth-DISPOSABLE-NO-TAILNET-ACCESS',
                                      'created': now.isoformat(), 'expires': (now+timedelta(hours=24)).isoformat(),
                                      'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': False,
                                        'preauthorized': True, 'tags': ['tag:guaranine-'+role]}}}}))
            key.chmod(0o600)
            args = [*config, 'node', '--role', role, '--name', 'disposable-'+role,
                    '--endpoint', 'https://100.64.0.1:6443', '--disk', '/dev/vda',
                    '--confirm-install-disk', '/dev/vda', '--secrets', str(scratch/'cluster/secrets.yaml'),
                    '--tailscale-key', str(key), '--output', str(scratch/role)]
            if role == 'controlplane':
                args += ['--etcd-disk-selector', 'disk.dev_path == "/dev/vdb"']
            subprocess.run(args, env=environment, check=True)
            subprocess.run([*images, 'private', '--config', str(scratch/role/'machine.yaml'),
                            '--format', image_format, '--output', str(scratch/(role+'-image'))], env=environment, check=True)
            print(f'Disposable private {image_format} built using upstream Talos imager; no enrollment qualification claimed', flush=True)


if __name__ == '__main__':
    main()
