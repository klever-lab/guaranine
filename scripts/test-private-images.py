#!/usr/bin/env python3
"""Exercise the operator command with disposable keys and the real upstream imager."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import time
from unittest.mock import patch

import yaml

import guaranine as cli


def disposable_key(token, tailnet, role, output, *, reusable=False):
    now = datetime.now(timezone.utc)
    cli.private_write(output, json.dumps({
        'id': 'disposable-ci', 'key': 'tskey-auth-DISPOSABLE-NO-TAILNET-ACCESS',
        'created': now.isoformat(), 'expires': (now + timedelta(hours=24)).isoformat(),
        'capabilities': {'devices': {'create': {'reusable': reusable, 'ephemeral': False,
                         'preauthorized': True, 'tags': ['tag:guaranine-' + role]}}}}))


def boot_shared_iso(image, directory):
    """Two isolated guests: maintenance, Tailscale startup, unique certificates, no disk writes."""
    guests = []
    certificates = set()
    try:
        for number in range(2):
            disk = directory / f'guest-{number}.raw'
            with disk.open('xb') as stream:
                stream.truncate(16 * 1024**3)
                stream.write(b'DISPOSABLE EXISTING DATA MUST SURVIVE ISO BOOT\n')
            before = cli.digest(disk)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            log = (directory / f'guest-{number}.log').open('xb')
            process = subprocess.Popen([
                'qemu-system-x86_64', '-accel', 'tcg', '-cpu', 'max', '-m', '1536', '-smp', '2',
                '-display', 'none', '-serial', 'stdio', '-monitor', 'none', '-no-reboot', '-boot', 'd',
                '-uuid', f'00000000-0000-4000-8000-{number + 1:012}', '-cdrom', str(image),
                '-drive', f'file={disk},format=raw,if=virtio',
                # Synthetic guest addresses exercise the subnet firewall without joining a tailnet.
                '-netdev', f'user,id=n0,restrict=on,net=100.64.0.0/24,host=100.64.0.2,dhcpstart=100.64.0.15,'
                           f'hostfwd=tcp:127.0.0.1:{port}-:50000',
                '-device', f'virtio-net-pci,netdev=n0,mac=52:54:00:00:00:{number + 1:02x}',
            ], stdout=log, stderr=subprocess.STDOUT)
            guests.append((process, log, disk, before, port))
        for process, log, disk, before, port in guests:
            deadline = time.monotonic() + 180
            connection = ['--insecure', '--nodes', f'127.0.0.1:{port}']
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    result = subprocess.run([cli.talosctl(), 'logs', 'ext-tailscale', *connection],
                                            capture_output=True, text=True, timeout=5)
                except subprocess.TimeoutExpired:
                    continue
                if result.returncode == 0 and 'Engine created' in result.stdout and 'tailscale0' in result.stdout:
                    break
                time.sleep(1)
            else:
                raise ValueError('The shared ISO did not start Tailscale with kernel networking in maintenance mode')
            result = subprocess.run([cli.talosctl(), 'get', 'machinestatus', *connection, '-o', 'json'],
                                    check=True, capture_output=True, text=True, timeout=10)
            if json.loads(result.stdout)['spec']['stage'] != 'maintenance':
                raise ValueError('The shared ISO left maintenance mode before operator authorization')
            disks = cli.read_disks(connection, directory)
            if not any(item['dev_path'] == '/dev/vda' for item in disks):
                raise ValueError('The shared ISO did not expose the guest disk for operator selection')
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE  # Isolated loopback fixture, never used by the operator command.
            with socket.create_connection(('127.0.0.1', port), timeout=5) as stream, context.wrap_socket(stream) as tls:
                certificates.add(tls.getpeercert(binary_form=True))
        if len(certificates) != 2:
            raise ValueError('Two boots of the same ISO reused a machine certificate')
    finally:
        for process, log, disk, before, port in guests:
            process.terminate()
        for process, log, disk, before, port in guests:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()
        if any(cli.digest(disk) != before for process, log, disk, before, port in guests):
            raise ValueError('The shared ISO modified an existing guest disk before operator confirmation')
    print('Same ISO booted twice: maintenance API and Tailscale started, certificates differed, disks unchanged', flush=True)


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='guaranine-disposable-private-images-') as scratch:
        directory = Path(scratch) / 'operator'
        with patch.dict(os.environ, GUARANINE_OPERATOR_DIR=str(directory)):
            cli.main(['init'])
            (directory / 'operator.yaml').write_text(yaml.safe_dump({
                'tailnet': 'disposable.invalid', 'tailscale_api_token': 'DISPOSABLE',
                'endpoint': 'https://100.64.0.1:6443'}))
            with patch.object(cli, 'mint', side_effect=disposable_key):
                cli.main(['iso'])
                cli.main(['controlplane', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda',
                             '--etcd-disk-selector', 'disk.dev_path == "/dev/vdb"'])
            boot_shared_iso(directory / 'installer/image/metal-amd64.iso', Path(scratch))
        print('Both operator commands built private images using upstream imager; no enrollment qualification claimed', flush=True)


if __name__ == '__main__':
    main()
