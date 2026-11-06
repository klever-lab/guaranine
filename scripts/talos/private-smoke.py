#!/usr/bin/env python3
"""Exercise the operator command with disposable keys and the real upstream imager."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import yaml

import config
from common import private_write


def disposable_key(token, tailnet, role, output):
    now = datetime.now(timezone.utc)
    private_write(output, json.dumps({
        'id': 'disposable-ci', 'key': 'tskey-auth-DISPOSABLE-NO-TAILNET-ACCESS',
        'created': now.isoformat(), 'expires': (now + timedelta(hours=24)).isoformat(),
        'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': False,
                         'preauthorized': True, 'tags': ['tag:guaranine-' + role]}}}}))


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='guaranine-disposable-private-images-') as scratch:
        directory = Path(scratch) / 'operator'
        with patch.dict(os.environ, GUARANINE_OPERATOR_DIR=str(directory)):
            config.main(['init'])
            (directory / 'operator.yaml').write_text(yaml.safe_dump({
                'tailnet': 'disposable.invalid', 'tailscale_api_token': 'DISPOSABLE',
                'endpoint': 'https://100.64.0.1:6443'}))
            with patch.object(config, 'mint', side_effect=disposable_key):
                config.main(['worker', 'disposable-worker', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda'])
                config.main(['controlplane', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda',
                             '--etcd-disk-selector', 'disk.dev_path == "/dev/vdb"'])
        print('Both operator commands built private images using upstream imager; no enrollment qualification claimed', flush=True)


if __name__ == '__main__':
    main()
