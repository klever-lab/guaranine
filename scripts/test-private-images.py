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
    args = {} if role is None else dict(name='fixture-' + role, role=role,
        cluster_endpoint='https://100.64.0.1:6443',
        token='DISPOSABLE-SERVER' if role == 'controlplane' else 'DISPOSABLE-AGENT', agent_token='DISPOSABLE-AGENT')
    config = cli.machine_config('tskey-auth-DISPOSABLE-NO-TAILNET-ACCESS', PUBLIC_KEY,
        wifi=None if role == 'controlplane' else 'update_config=0\np2p_disabled=1\nnetwork={\nssid=74657374\nkey_mgmt=NONE\n}\n', **args)
    checks = [
        'test -f /etc/guaranine/tailscale-key',
        f'grep -Fq "{PUBLIC_KEY}" /root/.ssh/authorized_keys',
        'timeout 120 sh -ec \'until systemctl is-active --quiet sshd guaranine-firewall tailscaled '
        'systemd-networkd systemd-resolved; do sleep 1; done\'',
        'timeout 60 sh -ec \'until /usr/lib/guaranine-network/ld-musl-x86_64.so.1 '
        '--library-path /usr/lib/guaranine-network /usr/lib/guaranine-network/nft '
        'get element inet guaranine lan4 "{ 10.0.2.0/24 }"; do sleep 1; done\'',
    ]
    if role == 'controlplane':
        checks += ['! mountpoint -q /var/lib/rancher/k3s', '! systemctl is-active --quiet k3s']
    elif role == 'worker':
        checks += ['! test -f /run/cos/live_mode', 'test "$(hostname)" = fixture-worker',
                   'test "$(cat /etc/rancher/k3s/token)" = DISPOSABLE-AGENT',
                   'test "$(stat -c %a /etc/rancher/k3s/token)" = 600',
                   '! test -f /etc/rancher/k3s/agent-token',
                   'grep -qx "ssid=74657374" /etc/guaranine/wifi.conf',
                   'grep -qx "server: https://100.64.0.1:6443" /etc/rancher/k3s/config.yaml.d/90-join.yaml',
                   'systemctl is-enabled --quiet k3s.service',
                   'systemctl show k3s.service --property=Environment --value | grep -q K3S_ROLE=agent']
    else:
        checks += ['test -f /run/cos/live_mode', 'test -f /etc/guaranine/wifi.conf',
                   '! test -f /etc/rancher/k3s/token', '! systemctl is-active --quiet k3s']
    config['stages']['boot'].append({'name': 'Disposable boot assertions', 'commands': [
        ' && '.join(checks) + ' && { ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub; '
        'echo GUARANINE_BOOT_OK; } > /dev/ttyS0']})
    return config


def boot(image, directory, number, *, cloud=False, install=False):
    disk = directory / f'data-{number}.raw'
    with disk.open('xb') as stream:
        stream.truncate(16 * 1024**3)
        stream.write(b'DISPOSABLE DATA MUST SURVIVE UNAUTHORIZED BOOT\n')
    before = None if install else cli.digest(disk)
    acceleration = ['-accel', 'kvm', '-cpu', 'host'] if os.access('/dev/kvm', os.R_OK | os.W_OK) else ['-accel', 'tcg', '-cpu', 'max']
    command = ['qemu-system-x86_64', *acceleration, '-m', '2048', '-smp', '2', '-display', 'none',
               '-serial', 'stdio', '-monitor', 'none', '-uuid', f'00000000-0000-4000-8000-{number:012}',
               '-netdev', 'user,id=n0,restrict=on', '-device', 'virtio-net-pci,netdev=n0']
    if not install:
        command.append('-no-reboot')
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
    elif image.suffix == '.iso':
        command += ['-boot', 'd', '-cdrom', str(image)]
    else:
        command += ['-drive', f'file={image},format=raw,if=virtio']
    command += ['-drive', f'file={disk},format=raw,if=virtio']
    log = directory / f'boot-{number}.log'
    try:
        with log.open('xb') as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + (600 if install else 240)
                while time.monotonic() < deadline:
                    status = process.poll()
                    if status is not None:
                        if install and status == 0:
                            return disk
                        break
                    text = log.read_text(errors='replace')
                    if not install and ('GUARANINE_BOOT_OK\r\n' in text or 'GUARANINE_BOOT_OK\n' in text):
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
        if not install and cli.digest(disk) != before:
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
            iso.unlink()
            vhd = cli.build_image(fixture('controlplane'), 'vhd', directory / 'vhd', source)
            boot(vhd, directory, 3, cloud=True)
            vhd.unlink()

            # Only this disposable fixture authorizes installation onto its QEMU disk.
            installer = fixture()
            installer['stages']['boot'].append({'name': 'Install the disposable worker',
                'files': [cli.config_file('/run/fixture-worker.yaml', cli.cloud_yaml(fixture('worker')))],
                'commands': ['kairos-agent --strict-validation manual-install --device /dev/vda /dev/stdin < /run/fixture-worker.yaml']})
            install_iso = cli.build_image(installer, 'iso', directory / 'install', source)
            worker = boot(install_iso, directory, 4, install=True)
            install_iso.unlink()
            boot(worker, directory, 5)
        except Exception:
            # This harness creates disposable credentials only. Keep failed CI diagnosable.
            logs = [*directory.glob('error-*.log'), *directory.glob('boot-*.log'),
                    directory / 'iso/build.log', directory / 'vhd/build.log', directory / 'install/build.log']
            for log in logs:
                if log.exists():
                    print(f'Fixture diagnostics ({log.name}):\n{log.read_text(errors="replace")[-8000:]}', file=sys.stderr)
            raise
    print('Shared ISO identities differ; VHD blocks missing data; worker installed, powered off and rebooted with its configuration; other guest disks unchanged')


if __name__ == '__main__':
    main()
