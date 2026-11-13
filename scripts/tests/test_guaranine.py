"""Private Kairos preparation, trust checks and destructive-operation boundaries."""
from datetime import datetime, timedelta, timezone
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import guaranine as cli


class PrivateConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def key(self, *, reusable=True, expires=None, created=None):
        now = datetime.now(timezone.utc)
        record = {'key': 'tskey-auth-DISPOSABLE', 'created': (created or now).isoformat(),
                  'expires': (expires or now + timedelta(hours=24)).isoformat(),
                  'capabilities': {'devices': {'create': {'reusable': reusable, 'ephemeral': False,
                      'preauthorized': True, 'tags': ['tag:guaranine-' + ('worker' if reusable else 'controlplane')]}}}}
        path = self.root / ('key-' + str(len(list(self.root.iterdir()))) + '.json')
        cli.private_write(path, json.dumps(record))
        return path

    def test_private_inputs_reject_symlinks_and_public_permissions(self):
        path = self.root / 'private'
        cli.private_write(path, 'SECRET')
        self.assertEqual(cli.protected(path), path)
        link = self.root / 'link'
        link.symlink_to(path)
        with self.assertRaises(ValueError): cli.protected(link)
        path.chmod(0o644)
        with self.assertRaises(ValueError): cli.protected(path)
        with self.assertRaises(ValueError): cli.external(ROOT / 'private')

    def test_expiry_role_and_small_clock_skew(self):
        now = datetime.now(timezone.utc)
        cli.auth_record(self.key(created=now + timedelta(seconds=0.4), expires=now + timedelta(hours=24)),
                        now=now, reusable=True)
        for key, reusable in [(self.key(expires=now - timedelta(seconds=1)), True),
                              (self.key(expires=now + timedelta(hours=25)), True),
                              (self.key(), False), (self.key(created=now + timedelta(seconds=10)), True)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                cli.auth_record(key, now=now, reusable=reusable)
        key = self.key()
        data = json.loads(key.read_text())
        data['capabilities']['devices']['create']['tags'] = ['tag:guaranine-controlplane']
        key.write_text(json.dumps(data))
        with self.assertRaises(ValueError): cli.auth_record(key, reusable=True)

    def test_only_tailnet_ipv4_endpoints_are_accepted(self):
        self.assertEqual(cli.endpoint('https://100.64.0.1:6443/'), 'https://100.64.0.1:6443')
        for value in ['https://127.0.0.1:6443', 'http://100.64.0.1:6443', 'https://10.0.0.1:6443',
                      'https://name.example:6443', 'https://u:p@100.64.0.1:6443',
                      'https://100.64.0.1:6443/path', 'https://100.64.0.1:6443?secret=1']:
            with self.subTest(value=value), self.assertRaises(ValueError): cli.endpoint(value)

    def test_native_roles_separate_live_media_worker_and_server_authority(self):
        base = yaml.safe_load((ROOT / 'kairos/config.yaml').read_text())
        def files(config):
            return {file['path']: file for stage in config['stages'].values()
                    for step in stage for file in step.get('files', [])}
        wifi = 'update_config=0\nnetwork={\n ssid=74657374\n key_mgmt=NONE\n}\n'
        live = cli.machine_config('tskey-auth-LIVE', 'ssh-ed25519 PUBLIC', wifi=wifi)
        self.assertEqual(base['install'], {'auto': False})
        self.assertTrue(base['hostname'].startswith('guaranine-live-'))
        self.assertEqual(live['ssh_authorized_keys'], ['ssh-ed25519 PUBLIC'])
        self.assertNotIn('k3s', cli.cloud_yaml(live))
        self.assertNotIn('device:', cli.cloud_yaml(live))
        self.assertFalse(any('k3s.service' in step.get('systemctl', {}).get('enable', [])
                             for step in base['stages']['initramfs']))
        worker = cli.machine_config('tskey-auth-WORKER', 'ssh-ed25519 PUBLIC', name='worker-one',
            role='worker', cluster_endpoint='https://100.64.0.1:6443', token='AGENT-ONLY', wifi=wifi)
        self.assertEqual(worker['hostname'], 'worker-one')
        self.assertTrue(worker['install']['poweroff'])
        self.assertEqual(worker['users'][0]['groups'], ['admin'])
        self.assertTrue(worker['users'][0]['lock_passwd'])
        text = cli.cloud_yaml(worker)
        self.assertIn('AGENT-ONLY', text)
        self.assertNotIn('SERVER-SECRET', text)
        self.assertNotIn('server/manifests', text)
        self.assertNotIn('GUARANINE_DATA', text)
        worker_files = files(worker)
        join = yaml.safe_load(worker_files['/etc/rancher/k3s/config.yaml.d/90-join.yaml']['content'])
        self.assertEqual(join, {'server': 'https://100.64.0.1:6443'})
        server = cli.machine_config('tskey-auth-SERVER', 'ssh-ed25519 PUBLIC',
            role='controlplane', token='SERVER-SECRET', agent_token='AGENT-ONLY')
        self.assertEqual(server['hostname'], cli.role_config('controlplane')['hostname'])
        text = cli.cloud_yaml(server)
        self.assertIn('GUARANINE_DATA', text)
        self.assertIn('var-lib-rancher-k3s.mount', text)
        self.assertIn('PodSecurityConfiguration', text)
        self.assertIn('restricted', text)
        self.assertNotIn('mkfs', text)
        self.assertNotIn('wifi.conf', text)
        common = yaml.safe_load(files(base)['/etc/rancher/k3s/config.yaml']['content'])
        server_files = files(server)
        config = yaml.safe_load(server_files['/etc/rancher/k3s/config.yaml.d/10-controlplane.yaml']['content'])
        self.assertEqual(common['flannel-iface'], 'tailscale0')
        self.assertTrue(config['secrets-encryption'])
        self.assertEqual(server_files[common['token-file']]['content'], 'SERVER-SECRET\n')
        self.assertEqual(server_files[config['agent-token-file']]['content'], 'AGENT-ONLY\n')
        self.assertEqual(server_files[config['agent-token-file']]['permissions'], 0o600)
        self.assertEqual(set(config['disable']), {'traefik', 'servicelb', 'local-storage', 'metrics-server', 'gateway-api-crd'})
        self.assertIn('BindsTo=var-lib-rancher-k3s.mount', server_files['/etc/systemd/system/k3s.service.d/role.conf']['content'])
        self.assertNotIn('node-name', common | config | join)
        self.assertNotIn('cluster-init', common | config)
        with self.assertRaises(ValueError):
            cli.machine_config('key', 'pub', name='server-one', role='controlplane', token='same', agent_token='same')

    def test_invalid_or_incomplete_roles_are_rejected(self):
        for arguments in ({'role': 'other'}, {'role': 'worker', 'token': 'AGENT'},
                          {'role': 'worker', 'name': 'worker-one'},
                          {'role': 'controlplane', 'token': 'SERVER'},
                          {'role': 'controlplane', 'name': 'INVALID', 'token': 'SERVER', 'agent_token': 'AGENT'}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                cli.machine_config('key', 'pub', **arguments)

    def test_failed_commands_keep_secret_diagnostics_private(self):
        result = subprocess.CompletedProcess([], 1, stdout='SECRET', stderr='PRIVATE')
        with patch.object(cli.subprocess, 'run', return_value=result), self.assertRaises(ValueError) as error:
            cli.private_command(['tool'], self.root, data='TOKEN')
        self.assertNotIn('SECRET', str(error.exception))
        log = next(self.root.glob('error-*.log'))
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.read_text(), 'SECRETPRIVATE')

    def test_tailscale_reboot_reuses_identity_without_replaying_registration_key(self):
        config = yaml.safe_load((ROOT / 'kairos/config.yaml').read_text())
        script = next(file['content'] for step in config['stages']['initramfs'] for file in step.get('files', [])
                      if file['path'] == '/etc/guaranine/tailscale-start')
        command = self.root / 'tailscale'
        cli.private_write(command, '#!/bin/sh\nif [ "$1" = status ]; then\n'
            'printf \'{"BackendState": "%s"}\\n\' "$FIXTURE_STATE"\nelse\n'
            'printf "%s\\n" "$@"\nfi\n')
        command.chmod(0o700)
        for state in ('Running', 'Stopped', 'NeedsLogin', 'NoState'):
            with self.subTest(state=state):
                output = subprocess.run(['sh', '-eu'], input=script, text=True, capture_output=True, check=True,
                    env={**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'], 'FIXTURE_STATE': state}).stdout
                self.assertEqual('--auth-key=file:/etc/guaranine/tailscale-key' in output, state in ('NeedsLogin', 'NoState'))


class WifiTests(unittest.TestCase):
    def read(self, ssid='test network', security='', password='', active=None, mode='infrastructure'):
        def run(arguments, _directory, **_kwargs):
            self.assertEqual(arguments[0], 'nmcli')
            if '--active' in arguments:
                return active if active is not None else '00000000-0000-0000-0000-000000000001:802-11-wireless\n'
            field = arguments[arguments.index('--get-values') + 1]
            return {'802-11-wireless.ssid': ssid, '802-11-wireless.mode': mode,
                    '802-11-wireless-security.key-mgmt': security,
                    '802-11-wireless-security.psk': password}[field] + '\n'
        with patch.object(cli, 'private_command', side_effect=run) as command:
            config = cli.operator_wifi(Path('/unused'))
        return config, command

    def test_open_network_and_injection_safe_ssid(self):
        ssid = 'wifi"\nnetwork={bad}'
        config, calls = self.read(ssid)
        self.assertIn('ssid=' + ssid.encode().hex(), config)
        self.assertIn('key_mgmt=NONE', config)
        self.assertNotIn(ssid, config)
        self.assertNotIn('ctrl_interface', config)
        self.assertNotIn('--show-secrets', str(calls.call_args_list))

    def test_wpa_password_is_derived_without_logging_or_hardware_bindings(self):
        config, calls = self.read(security='wpa-psk', password='DISPOSABLE-PASSWORD')
        expected = hashlib.pbkdf2_hmac('sha1', b'DISPOSABLE-PASSWORD', b'test network', 4096, 32).hex()
        self.assertIn('psk=' + expected, config)
        self.assertNotIn('DISPOSABLE-PASSWORD', config)
        self.assertNotIn('mac', config)
        self.assertNotIn('bssid', config)
        secret_calls = [call for call in calls.call_args_list if '--show-secrets' in call.args[0]]
        self.assertEqual(len(secret_calls), 1)
        self.assertIn('802-11-wireless-security.psk', secret_calls[0].args[0])

    def test_wpa3_and_raw_psk(self):
        config, _ = self.read(security='sae', password='quote"and\\slash')
        self.assertIn('key_mgmt=SAE', config)
        self.assertIn('ieee80211w=2', config)
        self.assertIn('sae_password="quote\\"and\\\\slash"', config)
        config, _ = self.read(security='wpa-psk', password='ab' * 32)
        self.assertIn('psk=' + 'ab' * 32, config)

    def test_missing_ambiguous_enterprise_and_bad_password_stop_before_build(self):
        for kwargs in [{'active': ''}, {'active': ('00000000-0000-0000-0000-000000000001:802-11-wireless\n' * 2)},
                       {'security': 'wpa-eap'}, {'security': 'wpa-psk', 'password': ''}, {'ssid': 'x' * 33},
                       {'mode': 'ap'}, {'security': 'sae', 'password': 'bad\npassword'}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.read(**kwargs)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        cli.private_write(self.root / 'ssh_key', 'DISPOSABLE')
        cli.private_write(self.root / 'ssh_key.pub', 'ssh-ed25519 PUBLIC')
        self.environment = patch.dict(os.environ, GUARANINE_OPERATOR_DIR=str(self.root))
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def worker_setup(self):
        cli.private_write(self.root / 'controlplane-known-hosts', 'DISPOSABLE')
        settings = {'tailnet': 'fixture.invalid', 'tailscale_api_token': 'DISPOSABLE',
                    'endpoint': 'https://100.64.0.1:6443'}
        cli.private_write(self.root / 'operator.yaml', yaml.safe_dump(settings))
        installer = cli.output_directory(self.root / 'installer')
        now = datetime.now(timezone.utc)
        cli.private_write(installer / 'tailscale-key.json', json.dumps({'key': 'tskey-auth-DISPOSABLE',
            'created': now.isoformat(), 'expires': (now + timedelta(hours=24)).isoformat(),
            'capabilities': {'devices': {'create': {'reusable': True, 'ephemeral': False,
                'preauthorized': True, 'tags': ['tag:guaranine-worker']}}}}))
        cli.private_write(installer / 'boot.yaml', cli.cloud_yaml(cli.machine_config('key', 'pub',
            wifi='update_config=0\nnetwork={\nssid=74657374\nkey_mgmt=NONE\n}\n')))
        return {'path': '/dev/vdb', 'size': 64 * 2**30, 'model': 'Fixture', 'serial': 'disk-b', 'tran': 'virtio'}

    def test_worker_uses_secure_agent_token_and_native_install_after_confirmation(self):
        disk = self.worker_setup()
        token = 'K10' + 'ab' * 32 + '::agent:DISPOSABLE'
        @contextlib.contextmanager
        def verified(*_args): yield ['ssh', 'PINNED-WORKER'], None
        def command(args, directory, **kwargs):
            if 'test -f /run/cos/live_mode' in args[-1]: return 'live'
            if 'kubectl get node' in args[-1]: return ''
            if 'server/agent-token' in args[-1]: return token
            self.assertIn('manual-install', args[-1])
            self.assertEqual(args[-1], 'kairos-agent --strict-validation manual-install --device /dev/vdb /dev/stdin')
            self.assertEqual(directory, self.root / 'nodes/worker-one')
            config = yaml.safe_load(kwargs['data'])
            self.assertTrue(config['install']['poweroff'])
            files = [file for stage in config['stages']['initramfs'] for file in stage.get('files', [])]
            self.assertEqual(next(f['content'] for f in files if f['path'].endswith('/token')).strip(), token)
            self.assertTrue(any(f['path'].endswith('/wifi.conf') for f in files))
            return ''
        with (patch.object(cli, 'verified_connection', verified), patch.object(cli, 'read_disks', return_value=[disk]),
              patch.object(cli, 'private_command', side_effect=command) as calls,
              patch('builtins.input', side_effect=['100.64.0.8', 'worker-one', '1', 'ERASE worker-one /dev/vdb']),
              contextlib.redirect_stdout(io.StringIO())):
            cli.install_worker(None)
        self.assertEqual(len([call for call in calls.call_args_list if 'manual-install' in call.args[0][-1]]), 1)
        self.assertEqual((self.root / 'nodes/worker-one/config.yaml').stat().st_mode & 0o777, 0o600)

    def test_cancelling_worker_or_changed_disk_never_sends_installation(self):
        disk = self.worker_setup()
        @contextlib.contextmanager
        def verified(*_args): yield ['ssh', 'PINNED-WORKER'], None
        for answer, disks in [('cancel', [[disk]]), ('ERASE worker-one /dev/vdb', [[disk], []])]:
            with (self.subTest(answer=answer), patch.object(cli, 'verified_connection', verified),
                  patch.object(cli, 'read_disks', side_effect=disks),
                  patch.object(cli, 'private_command', side_effect=['live', '']) as calls,
                  patch('builtins.input', side_effect=['100.64.0.8', 'worker-one', '1', answer]),
                  contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError)):
                cli.install_worker(None)
            self.assertFalse((self.root / 'nodes/worker-one').exists())
            self.assertFalse(any('manual-install' in call.args[0][-1] for call in calls.call_args_list))

    def test_missing_wifi_firmware_stops_before_registration_key_or_media(self):
        cli.private_write(self.root / 'operator.yaml', yaml.safe_dump({
            'tailnet': 'fixture.invalid', 'tailscale_api_token': 'DISPOSABLE', 'endpoint': None}))
        with (patch.object(cli, 'operator_wifi', return_value='DISPOSABLE'),
              patch.object(cli, 'private_command', return_value='linux'),
              patch.object(cli, 'mint') as mint, patch.object(cli, 'build_image') as image,
              patch.object(cli, 'build_base', side_effect=ValueError('Missing Wi-Fi firmware'))):
            with self.assertRaisesRegex(ValueError, 'Missing Wi-Fi firmware'): cli.prepare_iso(None)
        mint.assert_not_called()
        image.assert_not_called()
        self.assertFalse((self.root / 'installer').exists())

    def test_reused_iso_only_prints_laptop_steps_after_usb_write(self):
        self.worker_setup()
        image = cli.output_directory(self.root / 'installer/image') / 'guaranine-kairos.iso'
        cli.private_write(image, 'DISPOSABLE-ISO')
        for flashed in (False, True):
            output = io.StringIO()
            with (self.subTest(flashed=flashed), patch.object(cli, 'flash_iso', return_value=flashed),
                  patch.object(cli, 'build_base') as build, patch.object(cli, 'mint') as mint,
                  contextlib.redirect_stdout(output)):
                cli.prepare_iso(None)
            build.assert_not_called()
            mint.assert_not_called()
            self.assertEqual('Boot the laptop from this USB.' in output.getvalue(), flashed)
            self.assertEqual('./guaranine worker' in output.getvalue(), flashed)
            self.assertEqual('USB flashing was not completed.' in output.getvalue(), not flashed)

    def test_ssh_hostkey_requires_physical_fingerprint_and_is_pinned(self):
        blob = b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + b'\x11' * 32
        raw = '100.64.0.8 ssh-ed25519 ' + base64.b64encode(blob).decode() + '\n'
        fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
        result = subprocess.CompletedProcess([], 0, stdout=raw)
        with patch.object(cli.subprocess, 'run', return_value=result), patch('builtins.input', return_value=fingerprint):
            with cli.verified_connection('100.64.0.8', self.root) as (command, hosts):
                self.assertIn('StrictHostKeyChecking=yes', command)
                self.assertIn('GlobalKnownHostsFile=/dev/null', command)
                self.assertEqual(hosts.read_text(), raw)
                self.assertEqual(hosts.stat().st_mode & 0o777, 0o600)
            self.assertFalse(hosts.exists())
        with patch.object(cli.subprocess, 'run', return_value=result), patch('builtins.input', return_value='SHA256:WRONG'):
            with self.assertRaises(ValueError):
                with cli.verified_connection('100.64.0.8', self.root): self.fail('Untrusted SSH key accepted')

    def test_data_disk_with_signatures_is_never_formatted(self):
        disk = {'path': '/dev/vdb', 'size': 64 * 2**30, 'model': 'Fixture', 'serial': 'disk-b', 'tran': 'virtio'}
        with (patch.object(cli, 'read_disks', return_value=[disk]), patch('builtins.input', return_value='1'),
              patch.object(cli, 'private_command', side_effect=['', '{"signatures":[{"type":"xfs"}]}']) as command,
              contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError)):
            cli.initialize_data(['ssh'], self.root)
        self.assertFalse(any('mkfs' in str(call) for call in command.call_args_list))

    def test_existing_data_label_is_reused_without_selection_or_formatting(self):
        with patch.object(cli, 'private_command', return_value='present') as command, patch('builtins.input') as prompt:
            cli.initialize_data(['ssh'], self.root)
        prompt.assert_not_called()
        self.assertEqual(command.call_count, 1)

    def test_data_disk_signatures_are_checked_again_after_confirmation(self):
        disk = {'path': '/dev/vdb', 'size': 64 * 2**30, 'model': 'Fixture', 'serial': 'disk-b', 'tran': 'virtio'}
        for signatures in ('[]', '[{"type":"ext4"}]'):
            replies = ['', '{"signatures":[]}', '{"signatures":' + signatures + '}', '']
            with (self.subTest(signatures=signatures), patch.object(cli, 'read_disks', return_value=[disk]),
                  patch('builtins.input', side_effect=['1', 'INITIALIZE /dev/vdb']),
                  patch.object(cli, 'private_command', side_effect=replies) as command,
                  contextlib.redirect_stdout(io.StringIO())):
                if signatures == '[]':
                    cli.initialize_data(['ssh'], self.root)
                else:
                    with self.assertRaises(ValueError): cli.initialize_data(['ssh'], self.root)
            self.assertEqual(any('mkfs' in str(call) for call in command.call_args_list), signatures == '[]')

    def test_disk_inventory_excludes_boot_usb_readonly_and_nested_mounts(self):
        good = {'path': '/dev/vdb', 'type': 'disk', 'size': 64 * 2**30, 'ro': False, 'mountpoints': []}
        bad = {**good, 'path': '/dev/vda', 'children': [{'mountpoints': ['/usr/local']}]}
        with patch.object(cli, 'private_command', return_value=json.dumps({'blockdevices': [good, bad, {**good, 'ro': True}]})):
            self.assertEqual([disk['path'] for disk in cli.read_disks(['ssh'], self.root)], ['/dev/vdb'])


class USBFlashingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.image = Path(temporary.name) / 'guaranine-kairos.iso'
        cli.private_write(self.image, 'DISPOSABLE-ISO')
        self.disk = {'path': '/dev/sdy', 'type': 'disk', 'tran': 'usb', 'ro': False, 'size': 8000000000,
                     'model': 'Fixture USB', 'serial': 'DISPOSABLE-USB-A', 'label': None, 'maj:min': '8:96',
                     'mountpoints': [], 'children': [{'path': '/dev/sdy1', 'type': 'part', 'label': 'STUFF',
                                                     'maj:min': '8:97', 'mountpoints': ['/run/media/kian/STUFF']}]}
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(patch.object(cli.sys.stdin, 'isatty', return_value=True))
        stack.enter_context(patch.object(self.output, 'isatty', return_value=True))

    def test_inventory_excludes_internal_readonly_small_system_and_source_disks(self):
        candidates = [self.disk, {**self.disk, 'tran': 'nvme'}, {**self.disk, 'ro': True},
                      {**self.disk, 'size': 1}, {**self.disk, 'mountpoints': ['/']},
                      {**self.disk, 'mountpoints': ['/boot/efi']}, {**self.disk, 'mountpoints': ['[SWAP]']},
                      {**self.disk, 'children': [{'type': 'crypt'}]},
                      {**self.disk, 'children': [{'type': 'part', 'children': [{'type': 'lvm'}]}]}]
        source_disk = copy.deepcopy(self.disk)
        source = self.image.stat().st_dev
        source_disk['children'][0]['maj:min'] = f'{os.major(source)}:{os.minor(source)}'
        candidates.append(source_disk)
        result = subprocess.CompletedProcess(['lsblk'], 0, stdout=json.dumps({'blockdevices': candidates}))
        with patch.object(cli.subprocess, 'run', return_value=result) as run:
            self.assertEqual(cli.usb_disks(self.image), [self.disk])
        self.assertIn('--tree', run.call_args.args[0])

    def test_redirected_input_or_output_never_prompts_or_scans_devices(self):
        for stdin, stdout in ((False, True), (True, False), (False, False)):
            with (self.subTest(stdin=stdin, stdout=stdout),
                  patch.object(cli.sys.stdin, 'isatty', return_value=stdin),
                  patch.object(self.output, 'isatty', return_value=stdout),
                  patch.object(cli, 'usb_disks') as disks, patch('builtins.input') as prompt,
                  patch.object(cli.subprocess, 'run') as run):
                cli.flash_iso(self.image)
                disks.assert_not_called()
                prompt.assert_not_called()
                run.assert_not_called()

    def test_skip_cancellation_and_no_devices_never_unmount_or_write(self):
        for answers in ([''], ['0'], ['1', 'no'], [EOFError], [KeyboardInterrupt]):
            with (self.subTest(answers=answers), patch.object(cli, 'usb_disks', return_value=[self.disk]),
                  patch('builtins.input', side_effect=answers), patch.object(cli.subprocess, 'run') as run):
                self.assertFalse(cli.flash_iso(self.image))
                run.assert_not_called()
        with (patch.object(cli, 'usb_disks', return_value=[]), patch('builtins.input') as prompt,
              patch.object(cli.subprocess, 'run') as run):
            cli.flash_iso(self.image)
        prompt.assert_not_called()
        run.assert_not_called()

    def test_partial_confirmation_shows_full_required_text_without_writing(self):
        with (patch.object(cli, 'usb_disks', return_value=[self.disk]),
              patch('builtins.input', side_effect=['1', 'ERASE']),
              patch.object(cli.subprocess, 'run') as run):
            self.assertFalse(cli.flash_iso(self.image))
        run.assert_not_called()
        output = self.output.getvalue()
        self.assertIn('ENTIRE line below, including the space and device path:', output)
        self.assertIn('\n  ERASE /dev/sdy\n', output)
        self.assertIn('Confirmation did not match. The required text is: ERASE /dev/sdy', output)
        self.assertIn('Nothing was written to /dev/sdy.', output)

    def test_invalid_choices_never_unmount_or_write(self):
        for choice in ('2', '-1', 'invalid'):
            with (self.subTest(choice=choice), patch.object(cli, 'usb_disks', return_value=[self.disk]),
                  patch('builtins.input', return_value=choice), patch.object(cli.subprocess, 'run') as run,
                  self.assertRaises(ValueError)):
                cli.flash_iso(self.image)
            run.assert_not_called()

    def test_only_the_confirmed_usb_is_unmounted_and_written_as_a_whole_disk(self):
        second = copy.deepcopy(self.disk)
        second.update(path='/dev/sdz', serial='DISPOSABLE-USB-B')
        second['children'][0]['path'] = '/dev/sdz1'
        unmounted = copy.deepcopy(second)
        unmounted['children'][0]['mountpoints'] = []
        with (patch.object(cli, 'usb_disks', side_effect=[[self.disk, second], [self.disk, second], [unmounted]]),
              patch('builtins.input', side_effect=['2', 'ERASE /dev/sdz']), patch.object(cli.subprocess, 'run') as run):
            self.assertTrue(cli.flash_iso(self.image))
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['sudo', '--', 'umount', '--all-targets', '--', '/dev/sdz1'],
            ['sudo', '--', 'dd', f'if={self.image}', 'of=/dev/sdz', 'bs=4M', 'oflag=dsync',
             'status=progress', 'conv=fsync,nocreat']])
        self.assertIn('DISPOSABLE-USB-B', self.output.getvalue())
        self.assertIn('Writing ', self.output.getvalue())
        self.assertIn('Wait for the "Flashed" success message', self.output.getvalue())
        self.assertIn('Flashed guaranine-kairos.iso to /dev/sdz.', self.output.getvalue())

    def test_device_changes_before_or_after_unmount_never_start_a_write(self):
        changed = {**self.disk, 'serial': 'DIFFERENT-USB'}
        for reports in ([[self.disk], [changed]], [[self.disk], [self.disk], [changed]],
                        [[self.disk], [self.disk], [self.disk]]):
            with (self.subTest(reports=reports), patch.object(cli, 'usb_disks', side_effect=reports),
                  patch('builtins.input', side_effect=['1', 'ERASE /dev/sdy']),
                  patch.object(cli.subprocess, 'run') as run, self.assertRaises(ValueError)):
                cli.flash_iso(self.image)
            self.assertTrue(all('dd' not in call.args[0] for call in run.call_args_list))

    def test_unmount_or_write_failures_never_report_success(self):
        unmounted = copy.deepcopy(self.disk)
        unmounted['children'][0]['mountpoints'] = []
        for fail_unmount in (True, False):
            effects = [subprocess.CalledProcessError(1, ['umount'])] if fail_unmount else [
                subprocess.CompletedProcess(['umount'], 0), subprocess.CalledProcessError(1, ['dd'])]
            with (self.subTest(fail_unmount=fail_unmount),
                  patch.object(cli, 'usb_disks', side_effect=[[self.disk], [self.disk], [unmounted]]),
                  patch('builtins.input', side_effect=['1', 'ERASE /dev/sdy']),
                  patch.object(cli.subprocess, 'run', side_effect=effects) as run,
                  self.assertRaises(subprocess.CalledProcessError)):
                cli.flash_iso(self.image)
            self.assertEqual(run.call_count, 1 if fail_unmount else 2)
        self.assertNotIn('Flashed ', self.output.getvalue())
