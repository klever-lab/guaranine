#!/usr/bin/env python3
"""Build and boot disposable native Kairos images; never publish credentials or media."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import guaranine as cli


PUBLIC_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBERERERERERERERERERERERERERERERERERERERERER'


def fixture(role=None):
    args = {} if role is None else dict(name='fixture-server', role='controlplane',
        token='DISPOSABLE-SERVER', agent_token='DISPOSABLE-AGENT')
    config = cli.machine_config('tskey-auth-DISPOSABLE-NO-TAILNET-ACCESS', PUBLIC_KEY,
        wifi=None if role else 'update_config=0\np2p_disabled=1\nnetwork={\nssid=74657374\nkey_mgmt=NONE\n}\n', **args)
    checks = [
        'test -f /etc/guaranine/tailscale-key',
        'timeout 120 sh -ec \'until systemctl is-active --quiet sshd guaranine-firewall tailscaled; do sleep 1; done\'',
    ]
    if role:
        checks += ['! mountpoint -q /var/lib/rancher/k3s', '! systemctl is-active --quiet k3s']
    else:
        checks += ['test -f /run/cos/live_mode', 'test -f /etc/guaranine/wifi.conf',
                   '! test -f /etc/rancher/k3s/token', '! systemctl is-active --quiet k3s']
    config['stages']['boot'].append({'name': 'Disposable boot assertions', 'commands': [
        ' && '.join(checks) + ' && { ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub; '
        'echo GUARANINE_BOOT_OK; } > /dev/ttyS0']})
    return config


def boot(image, directory, number, *, cloud=False):
    disk = directory / f'data-{number}.raw'
    with disk.open('xb') as stream:
        stream.truncate(16 * 1024**3)
        stream.write(b'DISPOSABLE DATA MUST SURVIVE UNAUTHORIZED BOOT\n')
    before = cli.digest(disk)
    acceleration = ['-accel', 'kvm', '-cpu', 'host'] if os.access('/dev/kvm', os.R_OK | os.W_OK) else ['-accel', 'tcg', '-cpu', 'max']
    command = ['qemu-system-x86_64', *acceleration, '-m', '2048', '-smp', '2', '-display', 'none',
               '-serial', 'stdio', '-monitor', 'none', '-no-reboot', '-uuid', f'00000000-0000-4000-8000-{number:012}',
               '-netdev', 'user,id=n0,restrict=on', '-device', 'virtio-net-pci,netdev=n0']
    if cloud:
        pairs = [code for root in ('/usr/share/OVMF', '/usr/share/edk2/x64') for code in Path(root).glob('*CODE*.fd')
                 if 'secboot' not in code.resolve().name and Path(str(code).replace('CODE', 'VARS')).exists()]
        if not pairs:
            raise ValueError('Install OVMF to boot the Gen2 VHD fixture')
        code = pairs[0]
        variables = directory / f'uefi-{number}.fd'
        shutil.copyfile(Path(str(code).replace('CODE', 'VARS')), variables)
        command += ['-drive', f'if=pflash,format=raw,readonly=on,file={code}',
                    '-drive', f'if=pflash,format=raw,file={variables}', '-drive', f'file={image},format=vpc,if=virtio']
    else:
        command += ['-boot', 'd', '-cdrom', str(image)]
    command += ['-drive', f'file={disk},format=raw,if=virtio']
    log = directory / f'boot-{number}.log'
    try:
        with log.open('xb') as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline and process.poll() is None:
                    text = log.read_text(errors='replace')
                    if 'GUARANINE_BOOT_OK\r\n' in text or 'GUARANINE_BOOT_OK\n' in text:
                        fingerprints = re.findall(r'256 (SHA256:[A-Za-z0-9+/]+)', text)
                        if fingerprints:
                            return fingerprints[-1]
                    time.sleep(1)
                raise ValueError(f'Kairos boot assertions failed; protected diagnostics: {log}')
            finally:
                process.terminate()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    finally:
        if cli.digest(disk) != before:
            raise ValueError('Live/uninitialized cloud boot modified an existing guest data disk')


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='guaranine-image-tests-', ignore_cleanup_errors=True) as temporary:
        directory = Path(temporary)
        try:
            source = cli.build_base(directory)
            iso = cli.build_image(fixture(), 'iso', directory / 'iso', source)
            keys = {boot(iso, directory, 1), boot(iso, directory, 2)}
            if len(keys) != 2:
                raise ValueError('The same shared ISO reused an SSH host identity')
            vhd = cli.build_image(fixture('controlplane'), 'vhd', directory / 'vhd', source)
            boot(vhd, directory, 3, cloud=True)
        except Exception:
            # This harness creates disposable credentials only. Keep failed CI diagnosable.
            logs = [*directory.glob('error-*.log'), *directory.glob('boot-*.log'),
                    directory / 'iso/build.log', directory / 'vhd/build.log']
            for log in logs:
                if log.exists():
                    print(f'Fixture diagnostics ({log.name}):\n{log.read_text(errors="replace")[-8000:]}', file=sys.stderr)
            raise
    print('Disposable ISO booted twice with distinct identities; VHD booted with missing data mount blocked; guest disks unchanged')


if __name__ == '__main__':
    main()
