#!/usr/bin/env python3
"""Build and boot disposable native Kairos images; never publish credentials or media."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import guaranine as cli


PUBLIC_KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBERERERERERERERERERERERERERERERERERERERERER'


EFI_SEED = r'''#!/bin/sh
set -eu
test -d /sys/firmware/efi || exit 0
# File-only paths model stale entries whose original partitions no longer exist.
efibootmgr --create --disk /dev/vda --part 1 --file-dev-path --bootnum 8002 \
  --label 'Windows Boot Manager' --loader '\EFI\Microsoft\Boot\bootmgfw.efi'
efibootmgr --create --disk /dev/vda --part 1 --file-dev-path --bootnum 8001 \
  --label 'Linux Boot Manager' --loader '\EFI\systemd\systemd-bootx64.efi'
efibootmgr --create --disk /dev/vda --part 1 --file-dev-path --bootnum 8000 \
  --label ubuntu --loader '\EFI\ubuntu\shimx64.efi'
efibootmgr --bootnext 8000
entries=$(efibootmgr -v)
printf '%s\n' "$entries" > /dev/ttyS0
printf '%s\n' "$entries" | grep -q '^BootOrder: 8000,8001,8002,'
printf '%s\n' "$entries" | grep -q '^BootNext: 8000$'
echo GUARANINE_OLD_EFI_ENTRIES_OK > /dev/ttyS0
'''


EFI_CHECK = r'''#!/bin/sh
set -eu
test -d /sys/firmware/efi || exit 0
entries=$(efibootmgr -v)
printf '%s\n' "$entries" > /dev/ttyS0
order=$(printf '%s\n' "$entries" | awk '/^BootOrder:/ {print $2}')
first=${order%%,*}
test -n "$first"
entry=$(printf '%s\n' "$entries" | grep -E "^Boot$first\* Kairos[[:space:]]")
esp=$(readlink -f /dev/disk/by-label/COS_GRUB)
test "$(lsblk --noheadings --raw --output PKNAME "$esp")" = vda
part=$(lsblk --noheadings --raw --output PARTN "$esp")
uuid=$(lsblk --noheadings --raw --output PARTUUID "$esp")
test -n "$part" && test -n "$uuid"
printf '%s\n' "$entry" | grep -Fi "HD($part,GPT,$uuid,"
printf '%s\n' "$entry" | grep -Fi '/\EFI\boot\bootx64.efi'
printf '%s\n' "$entries" | grep -F 'Boot8000* ubuntu' | grep -F '\EFI\ubuntu\shimx64.efi'
printf '%s\n' "$entries" | grep -F 'Boot8001* Linux Boot Manager' | grep -F '\EFI\systemd\systemd-bootx64.efi'
printf '%s\n' "$entries" | grep -F 'Boot8002* Windows Boot Manager' | grep -F '\EFI\Microsoft\Boot\bootmgfw.efi'
if printf '%s\n' "$entries" | grep -q '^BootNext:'; then exit 1; fi
if test "$1" = boot; then
  current=$(printf '%s\n' "$entries" | awk '/^BootCurrent:/ {print $2}')
  test "$current" = "$first"
fi
echo GUARANINE_EFI_ENTRY_OK > /dev/ttyS0
'''


@contextmanager
def timed(label):
    started = time.monotonic()
    print(f'{label}: starting', flush=True)
    try:
        yield
    finally:
        print(f'{label}: {time.monotonic() - started:.1f}s elapsed', flush=True)


WIFI_CHECK = '''#!/bin/sh
set -eu
mkdir -p /run/fixture-tools
mount -t 9p -o trans=virtio,version=9p2000.L,ro fixture-tools /run/fixture-tools
tool() {
  tool_program=$1; shift
  /run/fixture-tools/ld-musl-x86_64.so.1 --library-path /run/fixture-tools "/run/fixture-tools/$tool_program" "$@"
}
ap() {
  ap_program=$1; shift
  tool ip netns exec fixture-wifi /run/fixture-tools/ld-musl-x86_64.so.1 \
    --library-path /run/fixture-tools "/run/fixture-tools/$ap_program" "$@"
}
connected() {
  attempts=0
  until tool iw dev wlan0 link | grep -q 'Connected to' && tool ip -4 -o address show wlan0 | grep -q '192.0.2.'; do
    attempts=$((attempts + 1)); test "$attempts" -lt 60 || return 1
    sleep 1
  done
}
trap 'result=$?; if test "$result" -ne 0; then journalctl -b -u wpa_supplicant@wlan0.service --no-pager -n 60; tool iw dev; tool ip address; fi; exit "$result"' EXIT
tool ip netns add fixture-wifi
modprobe mac80211_hwsim radios=2
udevadm settle
systemctl stop wpa_supplicant@wlan1.service
tool iw phy phy1 set netns name fixture-wifi
ap ip link set lo up
ap ip address add 192.0.2.1/24 dev wlan1
ap hostapd -B -P /run/fixture-hostapd.pid /run/fixture-hostapd.conf
ap dnsmasq --interface=wlan1 --bind-interfaces --port=0 --no-hosts --no-resolv \
  --user=root --group=root --pid-file=/run/fixture-dnsmasq.pid \
  --dhcp-range=192.0.2.10,192.0.2.30,255.255.255.0,5m --dhcp-option=3 --dhcp-option=6
connected
systemctl is-active --quiet wpa_supplicant@wlan0.service
/usr/lib/guaranine-network/ld-musl-x86_64.so.1 --library-path /usr/lib/guaranine-network \
  /usr/lib/guaranine-network/nft get element inet guaranine lan4 '{ 192.0.2.0/24 }'
kill "$(cat /run/fixture-hostapd.pid)"
attempts=0
while tool iw dev wlan0 link | grep -q 'Connected to'; do
  attempts=$((attempts + 1)); test "$attempts" -lt 20
  sleep 1
done
ap hostapd -B -P /run/fixture-hostapd.pid /run/fixture-hostapd.conf
connected
touch /run/fixture-wifi-ok
echo GUARANINE_WIFI_OK > /dev/ttyS0
'''


def wifi_tools(directory):
    """Test-only AP tools stay outside the production image and installed filesystem."""
    image = 'guaranine-fixture-wifi'
    script = '''set -eu
apk add --no-cache hostapd=2.11-r4 iw=6.17-r0 dnsmasq=2.91-r1 iproute2-minimal=6.17.0-r0 >/dev/null
mkdir /fixture-tools
for program in hostapd iw dnsmasq ip; do
  executable=$(command -v "$program")
  cp "$executable" /fixture-tools/
  ldd "$executable" | awk '$2 == "=>" && $3 ~ /^\\// {print $3} $1 ~ /^\\// {print $1}' |
    sort -u | while read -r library; do cp -L "$library" /fixture-tools/; done
done
tar -C /fixture-tools -cf - .
'''
    archive = directory / 'wifi-tools.tar'
    with archive.open('xb') as output, (directory / 'wifi-tools.log').open('xb') as errors:
        # Reuse the production stage already cached by build_base; don't pull its base a second time.
        subprocess.run(['sudo', '--', 'docker', 'build', '--platform', 'linux/amd64', '--target', 'wifi',
                        '--tag', image, str(cli.ROOT / 'kairos')],
                       stdout=errors, stderr=subprocess.STDOUT, check=True, timeout=300)
        subprocess.run(['sudo', '--', 'docker', 'run', '--pull=never', '--rm', '--log-driver', 'none', '--platform', 'linux/amd64',
                        '--entrypoint', '/bin/sh', image, '-ec', script],
                       stdout=output, stderr=errors, check=True, timeout=300)
    target = directory / 'wifi-tools'
    target.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(target, filter='data')
    archive.unlink()


def fixture(role=None):
    args = {} if role is None else dict(name='fixture-' + role, role=role,
        cluster_endpoint='https://100.64.0.1:6443',
        token='DISPOSABLE-SERVER' if role == 'controlplane' else 'DISPOSABLE-AGENT', agent_token='DISPOSABLE-AGENT')
    config = cli.machine_config('tskey-auth-DISPOSABLE-NO-TAILNET-ACCESS', PUBLIC_KEY,
        wifi=None if role == 'controlplane' else 'update_config=0\np2p_disabled=1\nnetwork={\nssid=74657374\nkey_mgmt=WPA-PSK\npsk="guaranine-disposable-wifi"\n}\n', **args)
    config['stages']['initramfs'].append({'name': 'Reserve the disposable serial port for test diagnostics',
        'systemctl': {'mask': ['serial-getty@ttyS0.service']}})
    assertion_files = []
    checks = [
        'test -f /etc/guaranine/tailscale-key',
        f'grep -Fq "{PUBLIC_KEY}" /root/.ssh/authorized_keys',
        'timeout 120 sh -ec \'until systemctl is-active --quiet sshd guaranine-firewall tailscaled '
        'systemd-networkd systemd-resolved systemd-logind; do sleep 1; done\'',
        'timeout 60 sh -ec \'until /usr/lib/guaranine-network/ld-musl-x86_64.so.1 '
        '--library-path /usr/lib/guaranine-network /usr/lib/guaranine-network/nft '
        'get element inet guaranine lan4 "{ 10.0.2.0/24 }"; do sleep 1; done\'',
        'k3s --version',
        'for driver in ath9k ath10k_pci iwlwifi iwldvm iwlmvm rtl8192ce rtl8192se rtl8187 b43 rtw88_8821ce; '
        'do modprobe "$driver" && modinfo "$driver" >/dev/null || exit 1; done',
        'for firmware in ucode5 ucode15 ucode16_mimo b0g0initvals5 lp0initvals15 n0initvals16; '
        'do test -s "/usr/lib/firmware/b43/$firmware.fw" || exit 1; done',
        'for firmware in iwlwifi-3160-17.ucode iwlwifi-7265-17.ucode iwlwifi-7265D-29.ucode '
        'iwlwifi-9000-pu-b0-jf-b0-46.ucode iwlwifi-Qu-b0-jf-b0-77.ucode '
        'iwlwifi-Qu-c0-jf-b0-77.ucode iwlwifi-QuZ-a0-jf-b0-77.ucode '
        'rtlwifi/rtl8192cfw.bin rtlwifi/rtl8192sefw.bin rtw88/rtw8821c_fw.bin '
        'ath10k/QCA9377/hw1.0/firmware-6.bin ath10k/QCA9377/hw1.0/board-2.bin '
        'ath10k/QCA6174/hw2.1/firmware-5.bin ath10k/QCA6174/hw3.0/firmware-6.bin '
        'ath10k/QCA6174/hw3.0/board-2.bin; '
        'do test -s "/usr/lib/firmware/$firmware.zst" || exit 1; done',
    ]
    lid_checks = [
        'busctl get-property org.freedesktop.login1 /org/freedesktop/login1 '
        f'org.freedesktop.login1.Manager {setting} | grep -qx \'s "ignore"\''
        for setting in ('HandleLidSwitch', 'HandleLidSwitchExternalPower', 'HandleLidSwitchDocked')
    ]
    checks += lid_checks
    if role == 'controlplane':
        config['stages']['mount-failure'] = [{
            'name': 'Report disposable controller mount failure',
            'commands': ['printf "%s\\n%s\\n" "$IMMUCORE_MOUNT_FAILURE_STEP" "$IMMUCORE_MOUNT_FAILURE" '
                         '> /dev/ttyS0; journalctl -b -u immucore --no-pager > /dev/ttyS0; '
                         'lsblk -o NAME,SIZE,TYPE,LABEL,PARTLABEL > /dev/ttyS0; blkid > /dev/ttyS0; '
                         'cat /oem/01_layout.yaml > /dev/ttyS0'],
        }]
        checks += ['! mountpoint -q /var/lib/rancher/k3s', '! systemctl is-active --quiet k3s']
    elif role == 'worker':
        checks += ['! test -f /run/cos/live_mode', 'test "$(hostname)" = fixture-worker',
                   '! test -e /run/guaranine-tailscale-live.env',
                   '! test -e /run/systemd/system/tailscaled.service.d/live.conf',
                   'test "$(cat /etc/rancher/k3s/token)" = DISPOSABLE-AGENT',
                   'test "$(stat -c %a /etc/rancher/k3s/token)" = 600',
                   '! test -f /etc/rancher/k3s/agent-token',
                   'grep -qx "ssid=74657374" /etc/guaranine/wifi.conf',
                   'grep -qx "server: https://100.64.0.1:6443" /etc/rancher/k3s/config.yaml.d/90-join.yaml',
                   'systemctl is-enabled --quiet k3s.service',
                   'systemctl show k3s.service --property=Environment --value | grep -q K3S_ROLE=agent']
        # Check firmware before poweroff as well: BootNext is consumed by reboot.
        config['stages'].setdefault('after-install', []).append({
            'name': 'Verify installed worker boot entry',
            'files': [cli.config_file('/run/fixture-efi-check', EFI_CHECK, 0o700)],
            'commands': ['sh /run/fixture-efi-check install && echo GUARANINE_INSTALL_OK > /dev/ttyS0']})
        config['stages']['boot'].append({
            'name': 'Verify the registered worker entry booted',
            'files': [cli.config_file('/run/fixture-efi-check', EFI_CHECK, 0o700)],
            'commands': ['sh /run/fixture-efi-check boot && touch /run/fixture-efi-ok']})
        checks += ['test -f /run/fixture-efi-ok']
        # Exercise the same activation script as NodeOp, including the v4.4.0 path-override bug.
        # The tripwire must remain untouched: ordinary boot stages must never run during a rollout.
        assertion_files.append(cli.config_file('/run/fixture-config-rollout',
            'set -x\n' + cli.rollout_script((cli.ROOT / 'kairos/lid.yaml').read_text(), '/oem/91_guaranine-lid.yaml',
                               host_root='/run/fixture-host'), 0o700))
        tripwire = cli.cloud_yaml({'stages': {stage: [{'name': 'Unrelated stage tripwire',
            'commands': ['touch /run/fixture-unrelated-stage-ran']}] for stage in ('initramfs', 'boot')}})
        checks += [
            "printf '%s' " + shlex.quote(tripwire) + ' > /oem/00_rollout-tripwire.yaml',
            "sed -i 's/=ignore/=suspend/g' /etc/systemd/logind.conf.d/10-guaranine.conf",
            'systemctl reload systemd-logind.service',
            'busctl get-property org.freedesktop.login1 /org/freedesktop/login1 '
            'org.freedesktop.login1.Manager HandleLidSwitch | grep -qx \'s "suspend"\'',
            'mount --make-rprivate /', 'mkdir /run/fixture-host', 'mount --rbind / /run/fixture-host',
            # A concurrent Wi-Fi check can unlink its netns bind target; detach the entire private tree.
            "sh -ec 'trap \"umount -l /run/fixture-host\" EXIT; "
            "unshare --pid --fork --mount-proc sh /run/fixture-config-rollout > /dev/ttyS0 2>&1'",
            '! mountpoint -q /run/fixture-host',
            'test ! -e /run/fixture-unrelated-stage-ran',
            'rm /oem/00_rollout-tripwire.yaml',
        ]
        checks += lid_checks
    else:
        checks += ['test -f /run/cos/live_mode', 'test -f /etc/guaranine/wifi.conf',
                   'grep -qx \'FLAGS="--state=mem:"\' /run/guaranine-tailscale-live.env',
                   'systemctl show tailscaled.service --property=EnvironmentFiles --value | '
                   'grep -q /run/guaranine-tailscale-live.env',
                   'tr \'\\0\' \'\\n\' < /proc/"$(systemctl show tailscaled.service --property=MainPID --value)"/cmdline | '
                   'grep -qx -- --state=mem:',
                   '! test -f /etc/rancher/k3s/token', '! systemctl is-active --quiet k3s']
    if role != 'controlplane':
        checks += ['test -f /run/fixture-wifi-ok', 'grep -qw efi=debug /proc/cmdline']
        config['stages']['boot'].append({'name': 'Verify WPA2 Wi-Fi, DHCP and AP recovery', 'files': [
            cli.config_file('/run/fixture-wifi-check', WIFI_CHECK, 0o700),
            cli.config_file('/run/fixture-hostapd.conf', 'interface=wlan1\ndriver=nl80211\nssid=test\n'
                'hw_mode=g\nchannel=6\nwpa=2\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\n'
                'wpa_passphrase=guaranine-disposable-wifi\n')],
            'commands': ['sh /run/fixture-wifi-check > /dev/ttyS0 2>&1']})
    config['stages']['boot'].append({'name': 'Disposable boot assertions', 'files': assertion_files, 'commands': [
        '{ set -x; ' + ' && '.join(checks) + ' && { ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub; '
        'echo GUARANINE_BOOT_OK; }; } > /dev/ttyS0 2>&1']})
    return config


def boot(image, directory, number, *, uefi=False, install=False, legacy_cpu=False, firmware=None):
    started = time.monotonic()
    acceleration = ['-accel', 'kvm', '-cpu', 'host'] if os.access('/dev/kvm', os.R_OK | os.W_OK) else ['-accel', 'tcg', '-cpu', 'max']
    if legacy_cpu:
        acceleration = ['-accel', 'tcg', '-cpu', 'Opteron_G1']
    print(f'Fixture {number}: {"UEFI" if uefi else "BIOS"} {"installation" if install else "boot"}; '
          f'accelerator={acceleration[1].upper()} cpu={acceleration[3]}', flush=True)
    disk = directory / f'data-{number}.raw'
    with disk.open('xb') as stream:
        stream.truncate(16 * 1024**3)
        stream.write(b'DISPOSABLE DATA MUST SURVIVE UNAUTHORIZED BOOT\n')
    before = None if install else cli.digest(disk)
    command = ['qemu-system-x86_64', *acceleration, '-m', '2048', '-smp', '2', '-display', 'none',
               '-serial', 'stdio', '-monitor', 'none', '-uuid', f'00000000-0000-4000-8000-{number:012}',
               '-netdev', 'user,id=n0,restrict=on', '-device', 'virtio-net-pci,netdev=n0']
    command += ['-fsdev', f'local,id=wifi,path={directory / "wifi-tools"},security_model=none,readonly=on',
                '-device', 'virtio-9p-pci,fsdev=wifi,mount_tag=fixture-tools']
    if not install:
        command.append('-no-reboot')
    if uefi:
        pairs = [code for root in ('/usr/share/OVMF', '/usr/share/edk2/x64') for code in Path(root).glob('*CODE*.fd')
                 if 'secboot' not in code.resolve().name and Path(str(code).replace('CODE', 'VARS')).exists()]
        if not pairs:
            raise ValueError('Install OVMF to boot the Gen2 VHD fixture')
        code = pairs[0]
        variables = firmware or directory / f'uefi-{number}.fd'
        if not variables.exists():
            shutil.copyfile(Path(str(code).replace('CODE', 'VARS')), variables)
        command += ['-drive', f'if=pflash,format=raw,readonly=on,file={code}',
                    '-drive', f'if=pflash,format=raw,file={variables}']
    if image.suffix == '.vhd':
        command += ['-drive', f'file={image},format=vpc,if=virtio']
    elif image.suffix == '.iso':
        command += ['-device', 'ich9-usb-ehci1,id=usb',
                    '-drive', f'if=none,id=installer,file={image},format=raw,readonly=on',
                    '-device', 'usb-storage,bus=usb.0,drive=installer,bootindex=1']
    else:
        command += ['-drive', f'file={image},format=raw,if=virtio']
    command += ['-drive', f'file={disk},format=raw,if=virtio']
    log = directory / f'boot-{number}.log'
    try:
        with log.open('xb') as output:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + (600 if install or legacy_cpu else 240)
                while time.monotonic() < deadline:
                    status = process.poll()
                    if status is not None:
                        text = log.read_text(errors='replace')
                        if install and status == 0 and re.search(r'^GUARANINE_INSTALL_OK\r?$', text, re.M):
                            if uefi and not re.search(r'^GUARANINE_OLD_EFI_ENTRIES_OK\r?$', text, re.M):
                                raise ValueError('UEFI installation did not seed the old boot entries')
                            print(f'Fixture {number}: guest powered off after installation', flush=True)
                            return disk
                        break
                    text = log.read_text(errors='replace')
                    if not install and re.search(r'^GUARANINE_BOOT_OK\r?$', text, re.M):
                        fingerprints = re.findall(r'256 (SHA256:[A-Za-z0-9+/]+)', text)
                        if fingerprints:
                            print(f'Fixture {number}: boot assertions passed', flush=True)
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
        try:
            if not install and cli.digest(disk) != before:
                raise ValueError('Live/uninitialized cloud boot modified an existing guest data disk')
        finally:
            print(f'Fixture {number}: {time.monotonic() - started:.1f}s elapsed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', choices=('all', 'live', 'controller', 'worker-bios', 'worker-uefi'),
                        default='all', help='Run one independent suite (default: all)')
    suite = parser.parse_args().suite
    os.umask(0o077)
    with (timed(f'Image suite {suite}'),
          tempfile.TemporaryDirectory(prefix='guaranine-image-tests-', ignore_cleanup_errors=True) as temporary):
        directory = Path(temporary)
        try:
            with timed('Base image build'):
                source = cli.build_base(directory)
            with timed('Test-only Wi-Fi tools'):
                wifi_tools(directory)
            if suite in ('all', 'live'):
                with timed('Live ISO build'):
                    iso = cli.build_image(fixture(), 'iso', directory / 'iso', source)
                keys = {boot(iso, directory, 1), boot(iso, directory, 2, legacy_cpu=True),
                        boot(iso, directory, 3, uefi=True)}
                if len(keys) != 3:
                    raise ValueError('The same shared ISO reused an SSH host identity')
                iso.unlink()
                print('Live suite passed: BIOS, legacy CPU and UEFI boots have distinct SSH identities.', flush=True)
            if suite in ('all', 'controller'):
                with timed('Controller VHD build'):
                    vhd = cli.build_image(fixture('controlplane'), 'vhd', directory / 'vhd', source)
                boot(vhd, directory, 4, uefi=True)
                vhd.unlink()
                print('Controller suite passed: missing data kept K3s stopped.', flush=True)
            if suite in ('all', 'worker-bios', 'worker-uefi'):
                # Only this disposable fixture authorizes installation onto its QEMU disk.
                installer = fixture()
                installer['stages']['boot'].append({'name': 'Install the disposable worker',
                    'files': [cli.config_file('/run/fixture-worker.yaml', cli.cloud_yaml(fixture('worker'))),
                              cli.config_file('/run/fixture-efi-seed', EFI_SEED, 0o700)],
                    'commands': ['test -f /run/fixture-wifi-ok && sh /run/fixture-efi-seed && '
                                 'kairos-agent --strict-validation manual-install '
                                 '--device /dev/vda /dev/stdin < /run/fixture-worker.yaml']})
                with timed('Worker installer ISO build'):
                    install_iso = cli.build_image(installer, 'iso', directory / 'install', source)
                if suite in ('all', 'worker-bios'):
                    worker = boot(install_iso, directory, 5, install=True)
                    boot(worker, directory, 6)
                    print('BIOS worker suite passed: installed, powered off and rebooted with its configuration.', flush=True)
                if suite in ('all', 'worker-uefi'):
                    firmware = directory / 'worker-uefi.fd'
                    worker = boot(install_iso, directory, 7, install=True, uefi=True, firmware=firmware)
                    boot(worker, directory, 8, uefi=True, firmware=firmware)
                    print('UEFI worker suite passed: Kairos took priority over old entries and booted with its configuration.', flush=True)
                install_iso.unlink()
        except Exception:
            # This harness creates disposable credentials only. Keep failed CI diagnosable.
            logs = [*directory.glob('error-*.log'), *directory.glob('boot-*.log'),
                    directory / 'iso/build.log', directory / 'vhd/build.log', directory / 'install/build.log',
                    directory / 'wifi-tools.log']
            for log in logs:
                if log.exists():
                    limit = 48000 if log.name.startswith('boot-') else 8000
                    print(f'Fixture diagnostics ({log.name}):\n{log.read_text(errors="replace")[-limit:]}', file=sys.stderr)
            raise
    print(f'Image suite {suite} passed; all selected Wi-Fi, configuration and disk-safety assertions passed.')


if __name__ == '__main__':
    main()
