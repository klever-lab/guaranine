#!/usr/bin/env python3
"""Boot the exact public Talos ISO in BIOS/UEFI; assert it cannot enroll or erase disks."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time

from common import PINS, digest, talosctl


def free_port():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        return server.getsockname()[1]


def test_firmware(iso, firmware, directory, binary):
    disk = directory / 'disk.qcow2'
    subprocess.run(['qemu-img', 'create', '-q', '-f', 'qcow2', str(disk), '16G'], check=True)
    original = digest(disk)
    port = free_port()
    args = ['qemu-system-x86_64', '-enable-kvm', '-cpu', 'host', '-m', '1536', '-smp', '2',
            '-display', 'none', '-serial', 'file:' + str(directory / 'serial.log'), '-monitor', 'none',
            '-drive', f'file={disk},format=qcow2,if=virtio', '-cdrom', str(iso), '-boot', 'd',
            '-netdev', f'user,id=internet,hostfwd=tcp:127.0.0.1:{port}-:50000',
            '-device', 'virtio-net-pci,netdev=internet']
    if firmware == 'uefi':
        code = next((path for path in (Path('/usr/share/OVMF/OVMF_CODE_4M.fd'), Path('/usr/share/edk2/x64/OVMF_CODE.4m.fd')) if path.exists()), None)
        template = next((path for path in (Path('/usr/share/OVMF/OVMF_VARS_4M.fd'), Path('/usr/share/edk2/x64/OVMF_VARS.4m.fd')) if path.exists()), None)
        if code is None or template is None:
            raise RuntimeError('Install OVMF firmware before running UEFI qualification')
        variables = directory / 'OVMF_VARS.fd'
        shutil.copyfile(template, variables)
        args.extend(['-drive', f'if=pflash,format=raw,readonly=on,file={code}',
                     '-drive', f'if=pflash,format=raw,file={variables}'])
    with (directory / 'qemu.log').open('wb') as log:
        process = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f'{firmware}: guest exited before the Talos API became available')
                try:
                    result = subprocess.run([binary, '--nodes', f'127.0.0.1:{port}', 'version', '--insecure'],
                                            capture_output=True, text=True, timeout=15)
                except subprocess.TimeoutExpired:
                    continue
                if result.returncode == 0 and result.stdout.count(PINS['talos']) >= 2:
                    break
                time.sleep(3)
            else:
                raise RuntimeError(f'{firmware}: Talos maintenance API did not become available')
        finally:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=10)
    if digest(disk) != original:
        raise RuntimeError(f'{firmware}: public media changed a target disk without a private configuration')
    return {'boot': 'passed', 'maintenance_api': 'passed', 'disk_unchanged': 'passed', 'talos_version': PINS['talos']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    if not os.access('/dev/kvm', os.R_OK | os.W_OK):
        parser.error('Actual-image qualification requires accessible KVM; it never silently skips')
    manifest = json.loads((args.directory / 'build.json').read_text())
    iso = args.directory / 'guaranine-talos-amd64.iso'
    if digest(iso) != PINS['iso']['sha256'] or manifest.get('private') is not False:
        parser.error('Only the exact pinned, credential-free public image can enter this qualification')
    binary = talosctl()
    evidence = {'source_commit': manifest['source_commit'], 'iso_sha256': digest(iso), 'firmware': {},
                'scope': 'Public image boot and maintenance API only; private enrollment and hosted tailnet are not certified'}
    with tempfile.TemporaryDirectory(prefix='guaranine-talos-qemu-') as scratch:
        for firmware in ('bios', 'uefi'):
            directory = Path(scratch) / firmware
            directory.mkdir(mode=0o700)
            evidence['firmware'][firmware] = test_firmware(iso, firmware, directory, binary)
            print(f'{firmware}: pinned Talos boots, maintenance API responds, and target disk is unchanged', flush=True)
    (args.directory / 'qemu-results.json').write_text(json.dumps(evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
