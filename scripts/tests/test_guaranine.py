"""Private Kairos preparation, trust checks and destructive-operation boundaries."""
from datetime import datetime, timedelta, timezone
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import guaranine as cli


class ImageHarnessTests(unittest.TestCase):
    def test_poweroff_requires_a_completed_install_marker_not_logged_command_text(self):
        spec = importlib.util.spec_from_file_location('image_harness', ROOT / 'scripts/test-private-images.py')
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        for marker, success in [('echo GUARANINE_INSTALL_OK\n', False),
                                 ('GUARANINE_INSTALL_OK extra\n', False),
                                 ('GUARANINE_INSTALL_OK\r\n', True)]:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                def qemu(_arguments, **kwargs):
                    kwargs['stdout'].write(marker.encode())
                    kwargs['stdout'].flush()
                    return SimpleNamespace(poll=lambda: 0, terminate=lambda: None, wait=lambda **_kwargs: 0)
                with (patch.object(harness.subprocess, 'Popen', side_effect=qemu),
                      contextlib.redirect_stdout(io.StringIO())):
                    if success:
                        self.assertEqual(harness.boot(directory / 'fixture.iso', directory, 1, install=True),
                                         directory / 'data-1.raw')
                    else:
                        with self.assertRaisesRegex(ValueError, 'boot assertions failed'):
                            harness.boot(directory / 'fixture.iso', directory, 1, install=True)


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

    def test_iso_source_tracks_commits_and_distinct_uncommitted_build_inputs(self):
        paths = ['guaranine', 'scripts/guaranine.py', 'requirements.txt',
                 'kairos/Dockerfile', 'kairos/worker.yaml']
        for relative in paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('DISPOSABLE source\n')
        def git(*arguments):
            return subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.root),
                *arguments], check=True, capture_output=True, text=True).stdout.strip()
        git('init', '--quiet')
        git('add', '.')
        def commit():
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '--quiet', '--allow-empty', '-m', 'Fixture')
        commit()
        with patch.object(cli, 'ROOT', self.root):
            original = cli.iso_source()
            self.assertEqual(original['commit'], git('rev-parse', 'HEAD'))
            self.assertEqual(cli.iso_source(), original)
            for relative in paths:
                with self.subTest(path=relative):
                    path = self.root / relative
                    contents = path.read_text()
                    path.write_text('first uncommitted edit\n')
                    first = cli.iso_source()
                    path.write_text('second uncommitted edit\n')
                    second = cli.iso_source()
                    self.assertEqual(first['commit'], original['commit'])
                    self.assertEqual(second['commit'], original['commit'])
                    self.assertNotEqual(original['sources_sha256'], first['sources_sha256'])
                    self.assertNotEqual(first['sources_sha256'], second['sources_sha256'])
                    path.write_text(contents)
            extra = self.root / 'kairos/new-input.yaml'
            extra.write_text('new untracked build input\n')
            self.assertNotEqual(cli.iso_source()['sources_sha256'], original['sources_sha256'])
            extra.unlink()
            self.assertEqual(cli.iso_source(), original)
            commit()
            self.assertNotEqual(cli.iso_source()['commit'], original['commit'])
            self.assertEqual(cli.iso_source()['sources_sha256'], original['sources_sha256'])

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
        self.assertEqual(worker['install']['grub_options']['extra_cmdline'], 'efi=debug')
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
        self.assertIsInstance(error.exception, cli.PrivateCommandError)
        self.assertEqual(error.exception.returncode, 1)
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
            if args[-1].startswith('if test ! -d /sys/firmware/efi'): return 'uefi'
            if 'cat /sys/class/dmi/id/product_name' in args[-1]: return 'Inspiron 5558\n'
            if args[-1] == 'k3s kubectl get nodes -o json': return '{"items":[]}'
            if args[-1].startswith('lsblk '): return json.dumps({'blockdevices': [disk]})
            if args[-1].startswith('timeout 600'):
                return json.dumps({'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                    'addresses': [{'type': 'InternalIP', 'address': '100.64.0.9'}]}})
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
        with (patch.object(cli, 'choose_laptop', return_value='100.64.0.8'),
              patch.object(cli, 'verified_connection', verified), patch.object(cli, 'read_disks', return_value=[disk]),
              patch.object(cli, 'private_command', side_effect=command) as calls,
              patch.object(cli.questionary, 'select') as select,
              patch('builtins.input', side_effect=['worker-one', 'ERASE']),
              contextlib.redirect_stdout(io.StringIO())):
            select.return_value.unsafe_ask.return_value = disk
            cli.install_worker(None)
        self.assertEqual(len([call for call in calls.call_args_list if 'manual-install' in call.args[0][-1]]), 1)
        self.assertEqual((self.root / 'nodes/worker-one/config.yaml').stat().st_mode & 0o777, 0o600)

    def test_cancelling_worker_or_changed_disk_never_sends_installation(self):
        disk = self.worker_setup()
        @contextlib.contextmanager
        def verified(*_args): yield ['ssh', 'PINNED-WORKER'], None
        for answer, disks in [('cancel', [[disk]]), ('ERASE', [[disk], []])]:
            with (self.subTest(answer=answer), patch.object(cli, 'choose_laptop', return_value='100.64.0.8'),
                  patch.object(cli, 'verified_connection', verified),
                  patch.object(cli, 'read_disks', side_effect=disks),
                  patch.object(cli, 'private_command', side_effect=['live', 'uefi', 'Inspiron 5558', '{"items":[]}']) as calls,
                  patch.object(cli.questionary, 'select') as select,
                  patch('builtins.input', side_effect=['worker-one', answer]),
                  contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError)):
                select.return_value.unsafe_ask.return_value = disk
                cli.install_worker(None)
            self.assertFalse((self.root / 'nodes/worker-one').exists())
            self.assertFalse(any('manual-install' in call.args[0][-1] for call in calls.call_args_list))

    def test_worker_disk_selection_cancels_before_erase_confirmation_or_installation(self):
        disk = self.worker_setup()
        @contextlib.contextmanager
        def verified(*_args): yield ['ssh', 'PINNED-WORKER'], None
        for answer in (False, None):
            with (self.subTest(answer=answer), patch.object(cli, 'choose_laptop', return_value='100.64.0.8'),
                  patch.object(cli, 'verified_connection', verified), patch.object(cli, 'read_disks', return_value=[disk]),
                  patch.object(cli, 'private_command', side_effect=['live', 'uefi', 'Inspiron 5558', '{"items":[]}']) as calls,
                  patch.object(cli.questionary, 'select') as select, patch('builtins.input', return_value='worker-one') as prompt,
                  contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'Installation cancelled')):
                select.return_value.unsafe_ask.return_value = answer
                cli.install_worker(None)
            prompt.assert_called_once()
            self.assertIn('Laptop name', prompt.call_args.args[0])
            self.assertFalse((self.root / 'nodes/worker-one').exists())
            self.assertFalse(any('manual-install' in call.args[0][-1] for call in calls.call_args_list))

    def test_full_worker_walkthrough_needs_no_ip_or_fingerprint_transcription(self):
        disk = {**self.worker_setup(), 'type': 'disk', 'ro': False, 'mountpoints': []}
        cli.output_directory(self.root / 'nodes/inspiron-5558-2')
        token = 'K10' + 'ab' * 32 + '::agent:DISPOSABLE'
        blob = b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + b'\x11' * 32
        key = '100.64.0.8 ssh-ed25519 ' + base64.b64encode(blob).decode() + '\n'
        prompts, sent = [], []
        answers = iter(['yes', '', 'ERASE'])
        confirmed = False
        def answer(prompt):
            nonlocal confirmed
            prompts.append(prompt)
            value = next(answers)
            confirmed = confirmed or value == 'ERASE'
            return value
        def command(arguments, directory, **kwargs):
            if arguments[0] == 'ssh-keygen':
                return '256 SHA256:DISPOSABLE no comment (ED25519)\n'
            if arguments[0] == 'tailscale':
                return json.dumps({'BackendState': 'Running', 'Peer': {'laptop': {
                    'HostName': 'guaranine-live-aaaa', 'Online': True, 'Tags': ['tag:guaranine-worker'],
                    'TailscaleIPs': ['100.64.0.8']}}})
            remote = arguments[-1]
            if 'live_mode' in remote: return 'live'
            if remote.startswith('if test ! -d /sys/firmware/efi'): return 'uefi'
            if remote == 'cat /sys/class/dmi/id/product_name': return 'Inspiron 5558\n'
            if remote == 'k3s kubectl get nodes -o json':
                return '{"items":[{"metadata":{"name":"inspiron-5558"}}]}'
            if remote.startswith('lsblk '): return json.dumps({'blockdevices': [disk]})
            if remote.startswith('cat /var/lib/rancher/k3s/server/agent-token'): return token
            if 'manual-install' in remote:
                self.assertTrue(confirmed)
                sent.append(remote)
                config = yaml.safe_load(kwargs['data'])
                self.assertEqual(config['hostname'], 'inspiron-5558-3')
                self.assertEqual(config['install']['grub_options']['extra_cmdline'], 'efi=debug')
                return ''
            if remote.startswith('timeout 600'):
                return json.dumps({'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
                    'addresses': [{'type': 'InternalIP', 'address': '100.64.0.90'}]}})
            self.fail('Unexpected command in install walkthrough: ' + remote)
        output = io.StringIO()
        with (patch.object(cli, 'private_command', side_effect=command),
              patch.object(cli.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=key)),
              patch.object(cli.questionary, 'select') as select,
              patch('builtins.input', side_effect=answer), contextlib.redirect_stdout(output)):
            select.return_value.unsafe_ask.side_effect = lambda: select.call_args.kwargs['choices'][1].value
            cli.install_worker(None)
        self.assertEqual(len(prompts), 3)
        self.assertEqual(select.call_count, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn('Found laptop', output.getvalue())
        self.assertIn('inspiron-5558-3 is Ready at 100.64.0.90', output.getvalue())
        self.assertNotIn(token, output.getvalue())
        self.assertFalse(any('IPv4 address' in prompt or 'fingerprint shown' in prompt for prompt in prompts))

    def test_unusable_uefi_stops_before_disk_selection_or_installation(self):
        self.worker_setup()
        @contextlib.contextmanager
        def verified(*_args): yield ['ssh', 'PINNED-WORKER'], None
        for status, message in [('outdated', 'USB needs updating'), ('unavailable', 'unavailable or read-only')]:
            with (self.subTest(status=status), patch.object(cli, 'choose_laptop', return_value='100.64.0.8'),
                  patch.object(cli, 'verified_connection', verified),
                  patch.object(cli, 'private_command', side_effect=['live', status]) as calls,
                  patch.object(cli, 'read_disks') as disks, patch.object(cli.questionary, 'select') as select,
                  patch('builtins.input') as prompt,
                  self.assertRaisesRegex(ValueError, message)):
                cli.install_worker(None)
            disks.assert_not_called()
            select.assert_not_called()
            prompt.assert_not_called()
            self.assertFalse(any('manual-install' in call.args[0][-1] for call in calls.call_args_list))

    def test_boot_preflight_shell_checks_tools_and_firmware_without_writing(self):
        with patch.object(cli, 'private_command', return_value='uefi') as call:
            cli.check_worker_boot(['ssh'], self.root)
        # Execute the actual remote shell; functions replace firmware/tools only.
        fixture = '''test() {
  case "$*" in
    '! -d /sys/firmware/efi') [ "$BOOT_FIXTURE" = bios ] ;;
    '-w /sys/firmware/efi/efivars') [ "$BOOT_FIXTURE" != unwritable ] ;;
    *) command test "$@" ;;
  esac
}
findmnt() {
  if [ "$BOOT_FIXTURE" = readonly ]; then printf ro; else printf rw,nosuid; fi
}
efibootmgr() {
  [ "$BOOT_FIXTURE" != outdated ] || return 1
  [ "${1-}" = --version ] || [ "$BOOT_FIXTURE" != unreadable ]
}
'''
        for mode, expected in [('bios', 'bios'), ('uefi', 'uefi'), ('outdated', 'outdated'),
                               ('readonly', 'unavailable'), ('unwritable', 'unavailable'), ('unreadable', 'unavailable')]:
            with self.subTest(mode=mode):
                result = subprocess.run(['sh', '-c', fixture + call.call_args.args[0][-1]],
                    check=True, capture_output=True, text=True, env={**os.environ, 'BOOT_FIXTURE': mode})
                self.assertEqual(result.stdout, expected)

    def test_native_label_collisions_on_other_disks_stop_installation(self):
        labels = [('label', value) for value in ('COS_GRUB', 'COS_OEM', 'COS_STATE', 'COS_RECOVERY', 'COS_PERSISTENT')]
        labels += [('partlabel', value) for value in ('efi', 'oem', 'state', 'recovery', 'persistent')]
        for field, value in labels:
            target = {'path': '/dev/vda', 'children': [{'path': '/dev/vda1', field: value}]}
            other = {'path': '/dev/vdb', 'children': [{'path': '/dev/vdb1', field: value}]}
            with self.subTest(field=field, value=value):
                for devices, collision in [([target], False), ([target, other], True)]:
                    raw = json.dumps({'blockdevices': devices})
                    with patch.object(cli, 'private_command', return_value=raw):
                        if collision:
                            with self.assertRaisesRegex(ValueError, '/dev/vdb1.*Nothing was erased'):
                                cli.check_install_labels(['ssh'], self.root, '/dev/vda')
                        else:
                            cli.check_install_labels(['ssh'], self.root, '/dev/vda')

    def test_definite_install_failure_stops_but_transport_loss_can_still_be_verified(self):
        disk = self.worker_setup()
        @contextlib.contextmanager
        def verified(*_args): yield ['ssh', 'PINNED-WORKER'], None
        for code in (1, 255, None):
            name = 'failure-' + str(code).lower()
            output = io.StringIO()
            def command(arguments, _directory, **_kwargs):
                remote = arguments[-1]
                if 'live_mode' in remote: return 'live'
                if remote.startswith('if test ! -d /sys/firmware/efi'): return 'uefi'
                if remote == 'cat /sys/class/dmi/id/product_name': return 'Inspiron 5558'
                if remote == 'k3s kubectl get nodes -o json': return '{"items":[]}'
                if remote.startswith('lsblk '): return json.dumps({'blockdevices': [disk]})
                if 'server/agent-token' in remote: return 'K10' + 'ab' * 32 + '::agent:DISPOSABLE'
                self.assertIn('manual-install', remote)
                if code is None: raise subprocess.TimeoutExpired(['ssh'], 1800)
                raise cli.PrivateCommandError(code, self.root / 'private.log')
            with (self.subTest(code=code), patch.object(cli, 'choose_laptop', return_value='100.64.0.8'),
                  patch.object(cli, 'verified_connection', verified), patch.object(cli, 'read_disks', return_value=[disk]),
                  patch.object(cli, 'private_command', side_effect=command),
                  patch.object(cli, 'wait_for_worker') as wait,
                  patch.object(cli.questionary, 'select') as select,
                  patch('builtins.input', side_effect=[name, 'ERASE']), contextlib.redirect_stdout(output)):
                select.return_value.unsafe_ask.return_value = disk
                if code == 1:
                    with self.assertRaisesRegex(ValueError, 'Installation failed.*Keep the USB connected'):
                        cli.install_worker(None)
                    wait.assert_not_called()
                    self.assertNotIn('remove the USB', output.getvalue())
                else:
                    cli.install_worker(None)
                    wait.assert_called_once()
                    self.assertIn('Could not confirm installation', output.getvalue())

    def test_conflicting_labels_prevent_native_install_even_after_confirmation(self):
        disk = self.worker_setup()
        @contextlib.contextmanager
        def verified(*_args): yield ['ssh', 'PINNED-WORKER'], None
        inventory = {'blockdevices': [disk, {'path': '/dev/vdc', 'children': [
            {'path': '/dev/vdc1', 'partlabel': 'efi'}]}]}
        with (patch.object(cli, 'choose_laptop', return_value='100.64.0.8'),
              patch.object(cli, 'verified_connection', verified), patch.object(cli, 'read_disks', return_value=[disk]),
              patch.object(cli, 'private_command', side_effect=[
                  'live', 'uefi', 'Inspiron 5558', '{"items":[]}', json.dumps(inventory)]) as calls,
              patch.object(cli.questionary, 'select') as select,
              patch('builtins.input', side_effect=['worker-one', 'ERASE']),
              contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, '/dev/vdc1.*Nothing was erased')):
            select.return_value.unsafe_ask.return_value = disk
            cli.install_worker(None)
        self.assertFalse((self.root / 'nodes/worker-one').exists())
        self.assertFalse(any('manual-install' in call.args[0][-1] for call in calls.call_args_list))

    def test_ready_check_can_resume_without_a_valid_installer_key(self):
        self.worker_setup()
        node = cli.output_directory(self.root / 'nodes/worker-one')
        cli.private_write(node / 'config.yaml', 'DISPOSABLE')
        ready = json.dumps({'status': {'conditions': [{'type': 'Ready', 'status': 'True'}],
            'addresses': [{'type': 'InternalIP', 'address': '100.64.0.90'}]}})
        with (patch.object(cli, 'auth_record') as auth, patch.object(cli, 'choose_laptop') as choose,
              patch.object(cli, 'private_command', return_value=ready) as command,
              contextlib.redirect_stdout(io.StringIO())):
            cli.install_worker(SimpleNamespace(wait='worker-one'))
        auth.assert_not_called()
        choose.assert_not_called()
        self.assertEqual(command.call_count, 1)
        self.assertIn('kubectl wait', command.call_args.args[0][-1])
        self.assertNotIn('manual-install', command.call_args.args[0][-1])

    def test_ready_check_rejects_unready_or_lan_address_and_explains_resume(self):
        for state, address in [('False', '100.64.0.9'), ('True', '192.168.1.2')]:
            raw = json.dumps({'status': {'conditions': [{'type': 'Ready', 'status': state}],
                'addresses': [{'type': 'InternalIP', 'address': address}]}})
            output = io.StringIO()
            with (self.subTest(state=state, address=address), patch.object(cli, 'private_command', return_value=raw),
                  contextlib.redirect_stdout(output), self.assertRaises(ValueError)):
                cli.wait_for_worker('worker-one', ['ssh'], self.root)
            self.assertNotIn(' is Ready at ', output.getvalue())
        with (patch.object(cli, 'private_command', side_effect=ValueError('DISPOSABLE failure')),
              contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, '--wait worker-one')):
            cli.wait_for_worker('worker-one', ['ssh'], self.root)

    def forget_setup(self, name='worker-one'):
        cli.private_write(self.root / 'controlplane-known-hosts', 'DISPOSABLE')
        cli.private_write(self.root / 'operator.yaml', yaml.safe_dump({
            'tailnet': 'fixture.invalid', 'tailscale_api_token': 'DISPOSABLE',
            'endpoint': 'https://100.64.0.1:6443'}))
        return cli.output_directory(self.root / 'nodes' / name)

    def test_worker_menu_add_requires_an_explicit_choice(self):
        output = io.StringIO()
        with (patch.object(cli, 'install_worker') as install, patch.object(cli, 'forget_worker') as forget,
              patch.object(cli, 'operator_settings') as settings, patch.object(cli.os, 'umask'),
              patch.object(cli.questionary, 'select') as select,
              patch('builtins.input') as prompt, contextlib.redirect_stdout(output)):
            select.return_value.unsafe_ask.return_value = 'add'
            cli.main(['worker'])
        self.assertEqual([choice.value for choice in select.call_args.kwargs['choices']], [False, 'add', 'forget'])
        select.return_value.unsafe_ask.assert_called_once()
        prompt.assert_not_called()
        install.assert_called_once()
        forget.assert_not_called()
        settings.assert_not_called()

    def test_worker_menu_cancellation_does_not_contact_nodes(self):
        for answer in (False, None, EOFError(), KeyboardInterrupt()):
            with (self.subTest(answer=answer), patch.object(cli, 'install_worker') as install,
                  patch.object(cli, 'forget_worker') as forget, patch.object(cli, 'operator_settings') as settings,
                  patch.object(cli, 'private_command') as command, patch.object(cli.os, 'umask'),
                  patch.object(cli.questionary, 'select') as select, patch('builtins.input') as prompt,
                  contextlib.redirect_stdout(io.StringIO()),
                  contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error):
                if isinstance(answer, BaseException):
                    select.return_value.unsafe_ask.side_effect = answer
                else:
                    select.return_value.unsafe_ask.return_value = answer
                cli.main(['worker'])
            self.assertEqual(error.exception.code, 1)
            prompt.assert_not_called()
            install.assert_not_called()
            forget.assert_not_called()
            settings.assert_not_called()
            command.assert_not_called()

    def test_worker_menu_enter_cancels_and_arrow_selection_adds_with_real_terminal_input(self):
        real_select = cli.questionary.select
        for keys, add in [('\r', False), ('\x1b[B\r', True)]:
            with self.subTest(keys=keys), create_pipe_input() as keyboard:
                keyboard.send_text(keys)
                def terminal_select(*args, **kwargs):
                    return real_select(*args, **kwargs, input=keyboard, output=DummyOutput())
                with (patch.object(cli.questionary, 'select', side_effect=terminal_select),
                      patch.object(cli, 'install_worker') as install, patch.object(cli, 'forget_worker') as forget,
                      patch.object(cli, 'operator_settings') as settings, patch('builtins.input') as prompt,
                      contextlib.redirect_stdout(io.StringIO())):
                    if add:
                        cli.manage_workers(SimpleNamespace(wait=None))
                    else:
                        with self.assertRaisesRegex(ValueError, 'cancelled'):
                            cli.manage_workers(SimpleNamespace(wait=None))
                self.assertEqual(install.call_count, int(add))
                forget.assert_not_called()
                settings.assert_not_called()
                prompt.assert_not_called()

    def test_forget_menu_without_saved_workers_does_not_load_controller_settings(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                if populated:
                    nodes = self.root / 'nodes'
                    cli.output_directory(nodes / cli.role_config('controlplane')['hostname'])
                    target = cli.output_directory(self.root / 'elsewhere')
                    (nodes / 'worker-link').symlink_to(target, target_is_directory=True)
                    cli.private_write(nodes / 'notes.log', 'DISPOSABLE')
                output = io.StringIO()
                with (patch.object(cli, 'operator_settings') as settings, patch.object(cli, 'forget_worker') as forget,
                      patch.object(cli, 'install_worker') as install, patch.object(cli.os, 'umask'),
                      patch.object(cli.questionary, 'select') as select,
                      patch('builtins.input') as prompt, contextlib.redirect_stdout(output)):
                    select.return_value.unsafe_ask.return_value = 'forget'
                    cli.main(['worker'])
                self.assertIn('No saved workers', output.getvalue())
                select.assert_called_once()
                prompt.assert_not_called()
                settings.assert_not_called()
                forget.assert_not_called()
                install.assert_not_called()

    def test_forget_menu_lists_workers_sorted_and_excludes_controller_files_and_links(self):
        chosen = self.forget_setup('zeta-worker')
        cli.output_directory(chosen.parent / 'alpha-worker')
        controller = cli.role_config('controlplane')['hostname']
        cli.output_directory(chosen.parent / controller)
        (chosen.parent / 'worker-link').symlink_to(chosen, target_is_directory=True)
        cli.private_write(chosen.parent / 'notes.log', 'DISPOSABLE')
        output = io.StringIO()
        with (patch.object(cli, 'forget_worker') as forget, patch.object(cli, 'install_worker') as install,
              patch.object(cli, 'private_command') as command, patch.object(cli.os, 'umask'),
              patch.object(cli.questionary, 'select') as select,
              patch('builtins.input') as prompt, contextlib.redirect_stdout(output)):
            select.return_value.unsafe_ask.side_effect = ['forget', 'zeta-worker']
            cli.main(['worker'])
        choices = select.call_args.kwargs['choices']
        self.assertEqual([choice.value for choice in choices], [False, 'alpha-worker', 'zeta-worker'])
        for excluded in (controller, 'worker-link', 'notes.log'):
            self.assertNotIn(excluded, [choice.title for choice in choices])
        prompt.assert_not_called()
        forget.assert_called_once()
        name, server, directory = forget.call_args.args
        self.assertEqual((name, directory), ('zeta-worker', self.root))
        self.assertIn('StrictHostKeyChecking=yes', server)
        self.assertIn('root@100.64.0.1', server)
        install.assert_not_called()
        command.assert_not_called()

    def test_forget_menu_cancellation_leaves_records_untouched(self):
        node = self.forget_setup()
        for answer in (False, None, EOFError(), KeyboardInterrupt()):
            with (self.subTest(answer=answer), patch.object(cli, 'forget_worker') as forget,
                  patch.object(cli, 'operator_settings') as settings, patch.object(cli, 'private_command') as command,
                  patch.object(cli.os, 'umask'), patch.object(cli.questionary, 'select') as select,
                  patch('builtins.input') as prompt,
                  contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
                  self.assertRaises(SystemExit) as error):
                select.return_value.unsafe_ask.side_effect = ['forget', answer]
                cli.main(['worker'])
            self.assertEqual(error.exception.code, 1)
            prompt.assert_not_called()
            forget.assert_not_called()
            settings.assert_not_called()
            command.assert_not_called()
            self.assertTrue(node.is_dir())
            self.assertFalse((self.root / 'backups').exists())

    def test_worker_wait_flag_bypasses_the_menu_and_installer(self):
        node = self.forget_setup()
        cli.private_write(node / 'config.yaml', 'DISPOSABLE')
        output = io.StringIO()
        with (patch.object(cli, 'wait_for_worker') as wait, patch.object(cli, 'forget_worker') as forget,
              patch.object(cli, 'auth_record') as auth, patch.object(cli, 'choose_laptop') as choose,
              patch.object(cli, 'private_command') as command, patch.object(cli.os, 'umask'),
              patch.object(cli.questionary, 'select') as select,
              patch('builtins.input') as prompt, contextlib.redirect_stdout(output)):
            cli.main(['worker', '--wait', 'worker-one'])
        prompt.assert_not_called()
        select.assert_not_called()
        forget.assert_not_called()
        auth.assert_not_called()
        choose.assert_not_called()
        command.assert_not_called()
        wait.assert_called_once()
        self.assertEqual((wait.call_args.args[0], wait.call_args.args[2]), ('worker-one', node))
        self.assertNotIn('Add worker', output.getvalue())

    def test_forget_command_archives_private_files_without_installer_or_laptop_access(self):
        node = self.forget_setup()
        contents = {'config.yaml': 'DISPOSABLE-AGENT-SECRET', 'error-install.log': 'DISPOSABLE diagnostics'}
        for name, text in contents.items():
            cli.private_write(node / name, text)
        unrelated = cli.output_directory(self.root / 'nodes/worker-two')
        output = io.StringIO()
        with (patch.object(cli, 'private_command', return_value='{"items":[]}') as command,
              patch.object(cli, 'auth_record') as auth, patch.object(cli, 'choose_laptop') as choose,
              patch.object(cli, 'verified_connection') as verified, patch.object(cli, 'wait_for_worker') as wait,
              patch.object(cli.os, 'umask'), patch.object(cli.questionary, 'select') as select,
              patch('builtins.input') as prompt,
              contextlib.redirect_stdout(output)):
            select.return_value.unsafe_ask.side_effect = ['forget', 'worker-one']
            cli.main(['worker'])
        auth.assert_not_called()
        choose.assert_not_called()
        verified.assert_not_called()
        wait.assert_not_called()
        self.assertEqual(select.return_value.unsafe_ask.call_count, 2)
        prompt.assert_not_called()
        command.assert_called_once()
        arguments = command.call_args.args[0]
        self.assertEqual(arguments[-1], 'k3s kubectl get nodes -o json')
        self.assertIn('StrictHostKeyChecking=yes', arguments)
        self.assertIn('root@100.64.0.1', arguments)
        self.assertFalse((self.root / 'installer').exists())
        self.assertFalse(node.exists())
        self.assertTrue(unrelated.is_dir())
        backups = self.root / 'backups'
        archive, = backups.iterdir()
        self.assertEqual(backups.stat().st_mode & 0o777, 0o700)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o700)
        for name, text in contents.items():
            self.assertEqual((archive / name).read_text(), text)
            self.assertEqual((archive / name).stat().st_mode & 0o777, 0o600)
        self.assertIn(str(archive), output.getvalue())
        self.assertNotIn('DISPOSABLE-AGENT-SECRET', output.getvalue())

    def test_forget_can_archive_an_empty_interrupted_attempt(self):
        node = self.forget_setup()
        with (patch.object(cli, 'private_command', return_value='{"items":[]}'),
              contextlib.redirect_stdout(io.StringIO())):
            cli.forget_worker('worker-one', ['ssh', 'PINNED'], self.root)
        self.assertFalse(node.exists())
        archive, = (self.root / 'backups').iterdir()
        self.assertEqual(list(archive.iterdir()), [])

    def test_forget_refuses_ready_and_notready_registered_workers(self):
        node = self.forget_setup()
        cli.private_write(node / 'config.yaml', 'DISPOSABLE')
        for ready in ('True', 'False'):
            registered = json.dumps({'items': [{'metadata': {'name': 'worker-one'},
                'status': {'conditions': [{'type': 'Ready', 'status': ready}]}}]})
            with (self.subTest(ready=ready), patch.object(cli, 'private_command', return_value=registered),
                  self.assertRaisesRegex(ValueError, 'registered in Kubernetes')):
                cli.forget_worker('worker-one', ['ssh', 'PINNED'], self.root)
            self.assertEqual((node / 'config.yaml').read_text(), 'DISPOSABLE')
            self.assertFalse((self.root / 'backups').exists())

    def test_forget_refuses_controller_without_contact_or_changes(self):
        name = cli.role_config('controlplane')['hostname']
        node = self.forget_setup(name)
        with (patch.object(cli, 'private_command') as command,
              self.assertRaisesRegex(ValueError, 'controller')):
            cli.forget_worker(name, ['ssh', 'PINNED'], self.root)
        command.assert_not_called()
        self.assertTrue(node.is_dir())
        self.assertFalse((self.root / 'backups').exists())

    def test_forget_keeps_records_when_controller_query_fails_or_is_invalid(self):
        node = self.forget_setup()
        cli.private_write(node / 'config.yaml', 'DISPOSABLE')
        for response in (ValueError('DISPOSABLE controller unavailable'), 'not JSON', '{}', '{"items":[{}]}'):
            with self.subTest(response=response):
                options = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
                with (patch.object(cli, 'private_command', **options),
                      self.assertRaises((ValueError, KeyError))):
                    cli.forget_worker('worker-one', ['ssh', 'PINNED'], self.root)
                self.assertEqual((node / 'config.yaml').read_text(), 'DISPOSABLE')
                self.assertFalse((self.root / 'backups').exists())

    def test_forget_rejects_source_and_backup_directory_symlinks(self):
        node = self.forget_setup()
        cli.private_write(node / 'config.yaml', 'DISPOSABLE')
        link = node.parent / 'worker-link'
        link.symlink_to(node, target_is_directory=True)
        with (patch.object(cli, 'private_command') as command,
              self.assertRaisesRegex(ValueError, 'worker directory')):
            cli.forget_worker('worker-link', ['ssh', 'PINNED'], self.root)
        command.assert_not_called()
        self.assertTrue(link.is_symlink())
        destination = cli.output_directory(self.root / 'elsewhere')
        (self.root / 'backups').symlink_to(destination, target_is_directory=True)
        with (patch.object(cli, 'private_command', return_value='{"items":[]}'),
              self.assertRaisesRegex(ValueError, 'Backup directory')):
            cli.forget_worker('worker-one', ['ssh', 'PINNED'], self.root)
        self.assertEqual((node / 'config.yaml').read_text(), 'DISPOSABLE')
        self.assertTrue((self.root / 'backups').is_symlink())
        self.assertEqual(list(destination.iterdir()), [])

    def test_worker_rejects_the_removed_forget_flag(self):
        with (patch.object(cli, 'install_worker') as install, patch.object(cli.os, 'umask'),
              contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error):
            cli.main(['worker', '--forget', 'worker-one'])
        self.assertEqual(error.exception.code, 2)
        install.assert_not_called()

    def test_name_prompt_accepts_a_failed_name_after_forget_in_another_terminal(self):
        disk = self.worker_setup()
        node = cli.output_directory(self.root / 'nodes/worker-one')
        cli.private_write(node / 'config.yaml', 'DISPOSABLE')
        @contextlib.contextmanager
        def verified(*_args): yield ['ssh', 'PINNED-WORKER'], None
        replies = iter(['worker-one', 'worker-one', 'cancel'])
        names = 0
        def answer(prompt):
            nonlocal names
            if prompt.startswith('Laptop name'):
                names += 1
                if names == 2:
                    cli.forget_worker('worker-one', ['ssh', 'PINNED'], self.root)
            return next(replies)
        output = io.StringIO()
        with (patch.object(cli, 'choose_laptop', return_value='100.64.0.8'),
              patch.object(cli, 'verified_connection', verified), patch.object(cli, 'read_disks', return_value=[disk]),
              patch.object(cli, 'private_command', side_effect=[
                  'live', 'uefi', 'Inspiron 5558', '{"items":[]}', '{"items":[]}']) as command,
              patch.object(cli.questionary, 'select') as select,
              patch('builtins.input', side_effect=answer), contextlib.redirect_stdout(output),
              self.assertRaisesRegex(ValueError, 'Installation cancelled')):
            select.return_value.unsafe_ask.return_value = disk
            cli.install_worker(None)
        self.assertIn('./guaranine worker', output.getvalue())
        self.assertIn('Forget worker', output.getvalue())
        self.assertEqual(names, 2)
        self.assertFalse(node.exists())
        archive, = (self.root / 'backups').iterdir()
        self.assertEqual((archive / 'config.yaml').read_text(), 'DISPOSABLE')
        self.assertFalse(any('manual-install' in call.args[0][-1] for call in command.call_args_list))

    def test_iso_rebuild_keeps_network_and_key_and_preserves_previous_image(self):
        self.worker_setup()
        image = cli.output_directory(self.root / 'installer/image') / 'guaranine-kairos.iso'
        cli.private_write(image, 'OLD-DISPOSABLE-ISO')
        receipt = image.parent / 'source.json'
        current = {'commit': 'a' * 40, 'sources_sha256': 'b' * 64}
        boot = (self.root / 'installer/boot.yaml').read_text()
        key = (self.root / 'installer/tailscale-key.json').read_text()
        cases = [('missing metadata', None, False), ('invalid metadata', '{', False),
                 ('invalid metadata encoding', b'\xff', False),
                 ('new commit', json.dumps({**current, 'commit': 'c' * 40}), False),
                 ('uncommitted edit', json.dumps({**current, 'sources_sha256': 'd' * 64}), False),
                 ('explicit rebuild', json.dumps(current), True)]
        for reason, previous_source, forced in cases:
            with self.subTest(reason=reason):
                receipt.unlink(missing_ok=True)
                if previous_source is not None:
                    cli.private_write(receipt, '')
                    receipt.write_bytes(previous_source if isinstance(previous_source, bytes) else previous_source.encode())
                image.write_text('OLD-DISPOSABLE-ISO')
                previous_archives = set(image.parent.parent.glob('image-previous-*'))
                def build(config, kind, directory, source):
                    self.assertEqual(config, yaml.safe_load(boot))
                    self.assertEqual((kind, source), ('iso', 'PUBLIC-BASE'))
                    self.assertEqual(image.read_text(), 'OLD-DISPOSABLE-ISO')
                    self.assertEqual(set(image.parent.parent.glob('image-previous-*')), previous_archives)
                    cli.output_directory(directory)
                    cli.private_write(directory / image.name, 'NEW-DISPOSABLE-ISO')
                    return directory / image.name
                with (patch.object(cli, 'iso_source', return_value=current),
                      patch.object(cli, 'build_base', return_value='PUBLIC-BASE') as base,
                      patch.object(cli, 'build_image', side_effect=build) as builder,
                      patch.object(cli, 'mint') as mint, patch.object(cli, 'operator_wifi') as wifi,
                      patch.object(cli, 'flash_iso', return_value=False), contextlib.redirect_stdout(io.StringIO())):
                    cli.prepare_iso(SimpleNamespace(rebuild=forced))
                base.assert_called_once()
                builder.assert_called_once()
                mint.assert_not_called()
                wifi.assert_not_called()
                self.assertEqual((self.root / 'installer/boot.yaml').read_text(), boot)
                self.assertEqual((self.root / 'installer/tailscale-key.json').read_text(), key)
                self.assertEqual(image.read_text(), 'NEW-DISPOSABLE-ISO')
                self.assertEqual(json.loads(cli.protected(receipt).read_text()), current)
                archives = set(image.parent.parent.glob('image-previous-*')) - previous_archives
                self.assertEqual(len(archives), 1)
                self.assertEqual((archives.pop() / image.name).read_text(), 'OLD-DISPOSABLE-ISO')

    def test_failed_iso_rebuild_keeps_old_image_and_can_retry(self):
        self.worker_setup()
        image = cli.output_directory(self.root / 'installer/image') / 'guaranine-kairos.iso'
        cli.private_write(image, 'OLD-DISPOSABLE-ISO')
        source = {'commit': 'a' * 40, 'sources_sha256': 'b' * 64}
        def build(_config, _kind, directory, _source):
            cli.output_directory(directory)
            if not list(directory.parent.glob('image-build-*/build.log')):
                cli.private_write(directory / 'build.log', 'DISPOSABLE failure diagnostics')
                raise ValueError('DISPOSABLE failed rebuild')
            cli.private_write(directory / image.name, 'NEW-DISPOSABLE-ISO')
            return directory / image.name
        with (patch.object(cli, 'iso_source', return_value=source),
              patch.object(cli, 'build_base', return_value='PUBLIC-BASE'),
              patch.object(cli, 'build_image', side_effect=build), patch.object(cli, 'mint') as mint,
              patch.object(cli, 'operator_wifi') as wifi, patch.object(cli, 'flash_iso', return_value=False) as flash,
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaisesRegex(ValueError, 'failed rebuild'):
                cli.prepare_iso(None)
            flash.assert_not_called()
            self.assertEqual(image.read_text(), 'OLD-DISPOSABLE-ISO')
            self.assertFalse((image.parent / 'source.json').exists())
            self.assertFalse(list(image.parent.parent.glob('image-previous-*')))
            cli.prepare_iso(None)
        mint.assert_not_called()
        wifi.assert_not_called()
        self.assertEqual(image.read_text(), 'NEW-DISPOSABLE-ISO')
        self.assertEqual(json.loads((image.parent / 'source.json').read_text()), source)

    def test_source_change_during_iso_build_never_flashes_or_certifies_output(self):
        self.worker_setup()
        image = cli.output_directory(self.root / 'installer/image') / 'guaranine-kairos.iso'
        cli.private_write(image, 'OLD-DISPOSABLE-ISO')
        source = {'commit': 'a' * 40, 'sources_sha256': 'b' * 64}
        current = [source]
        def build(_config, _kind, directory, _source):
            cli.output_directory(directory)
            cli.private_write(directory / image.name, 'MIXED-DISPOSABLE-ISO')
            current[0] = {**source, 'sources_sha256': 'c' * 64}
            return directory / image.name
        with (patch.object(cli, 'iso_source', side_effect=lambda: current[0]),
              patch.object(cli, 'build_base', return_value='PUBLIC-BASE'),
              patch.object(cli, 'build_image', side_effect=build), patch.object(cli, 'flash_iso') as flash,
              contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError)):
            cli.prepare_iso(None)
        flash.assert_not_called()
        self.assertEqual(image.read_text(), 'OLD-DISPOSABLE-ISO')
        self.assertFalse(list(image.parent.parent.glob('*/source.json')))
        self.assertFalse(list(image.parent.parent.glob('image-previous-*')))

    def test_initial_iso_build_records_source_before_offering_flash(self):
        self.worker_setup()
        (self.root / 'installer/boot.yaml').unlink()
        source = {'commit': 'a' * 40, 'sources_sha256': 'b' * 64}
        def build(_config, kind, directory, base):
            self.assertEqual((kind, base), ('iso', 'PUBLIC-BASE'))
            cli.output_directory(directory)
            image = directory / 'guaranine-kairos.iso'
            cli.private_write(image, 'NEW-DISPOSABLE-ISO')
            return image
        def flash(image):
            self.assertEqual(json.loads(cli.protected(image.parent / 'source.json').read_text()), source)
            return False
        with (patch.object(cli, 'iso_source', return_value=source),
              patch.object(cli, 'operator_wifi', return_value='DISPOSABLE-WIFI'),
              patch.object(cli, 'private_command', return_value='linux'),
              patch.object(cli, 'build_base', return_value='PUBLIC-BASE'),
              patch.object(cli, 'build_image', side_effect=build), patch.object(cli, 'flash_iso', side_effect=flash),
              contextlib.redirect_stdout(io.StringIO())):
            cli.prepare_iso(None)

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

    def test_laptop_discovery_waits_and_selects_only_online_tagged_live_peers(self):
        first = {'HostName': 'guaranine-live-aaaa', 'Online': True, 'Tags': ['tag:guaranine-worker'],
                 'TailscaleIPs': ['fd7a:115c:a1e0::8', '100.64.0.8']}
        second = {**first, 'HostName': 'guaranine-live-bbbb', 'TailscaleIPs': ['100.64.0.9']}
        peers = {'second': second, 'first': first,
                 'offline': {**first, 'Online': False},
                 'installed': {**first, 'HostName': 'worker-one'},
                 'controller': {**first, 'Tags': ['tag:guaranine-controlplane']},
                 'untagged': {**first, 'Tags': []}}
        output = io.StringIO()
        with (patch.object(cli, 'private_command', side_effect=[
                  json.dumps({'BackendState': 'Running', 'Peer': None}),
                  json.dumps({'BackendState': 'Running', 'Peer': peers})]) as command,
              patch.object(cli.time, 'sleep') as sleep, patch.object(cli.questionary, 'select') as select,
              patch('builtins.input') as prompt,
              contextlib.redirect_stdout(output)):
            select.return_value.unsafe_ask.return_value = '100.64.0.9'
            self.assertEqual(cli.choose_laptop(self.root), '100.64.0.9')
        sleep.assert_called_once_with(2)
        self.assertTrue(all(call.args[0] == ['tailscale', 'status', '--json'] for call in command.call_args_list))
        self.assertIn('Boot the laptop from the Guaranine USB', output.getvalue())
        choices = select.call_args.kwargs['choices']
        self.assertEqual([choice.value for choice in choices], [False, '100.64.0.8', '100.64.0.9'])
        self.assertIn('guaranine-live-aaaa', choices[1].title)
        self.assertIn('guaranine-live-bbbb', choices[2].title)
        prompt.assert_not_called()
        self.assertNotIn('worker-one', output.getvalue())

    def test_laptop_selection_cancellation_never_contacts_a_node(self):
        self.worker_setup()
        peer = {'HostName': 'guaranine-live-aaaa', 'Online': True, 'Tags': ['tag:guaranine-worker'],
                'TailscaleIPs': ['100.64.0.8']}
        second = {**peer, 'HostName': 'guaranine-live-bbbb', 'TailscaleIPs': ['100.64.0.9']}
        status = json.dumps({'BackendState': 'Running', 'Peer': {'laptop': peer, 'second': second}})
        for answer in (False, None):
            with (self.subTest(answer=answer), patch.object(cli, 'private_command', return_value=status) as command,
                  patch.object(cli, 'verified_connection') as connect, patch.object(cli.questionary, 'select') as select,
                  patch('builtins.input') as prompt,
                  contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'selection cancelled')):
                select.return_value.unsafe_ask.return_value = answer
                cli.install_worker(None)
            connect.assert_not_called()
            prompt.assert_not_called()
            self.assertEqual([call.args[0] for call in command.call_args_list], [['tailscale', 'status', '--json']])

    def test_laptop_discovery_explains_disconnected_operator(self):
        with (patch.object(cli, 'private_command', return_value='{"BackendState":"NeedsLogin"}'),
              patch.object(cli.time, 'sleep') as sleep, patch('builtins.input') as prompt,
              contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'Connect this computer')):
            cli.choose_laptop(self.root)
        sleep.assert_not_called()
        prompt.assert_not_called()

    def test_reused_iso_only_prints_laptop_steps_after_usb_write(self):
        self.worker_setup()
        image = cli.output_directory(self.root / 'installer/image') / 'guaranine-kairos.iso'
        cli.private_write(image, 'DISPOSABLE-ISO')
        source = {'commit': 'a' * 40, 'sources_sha256': 'b' * 64}
        cli.private_write(image.parent / 'source.json', json.dumps(source))
        for flashed in (False, True):
            output = io.StringIO()
            with (self.subTest(flashed=flashed), patch.object(cli, 'flash_iso', return_value=flashed),
                  patch.object(cli, 'iso_source', return_value=source),
                  patch.object(cli, 'build_base') as build, patch.object(cli, 'mint') as mint,
                  contextlib.redirect_stdout(output)):
                cli.prepare_iso(None)
            build.assert_not_called()
            mint.assert_not_called()
            self.assertEqual('Boot the laptop from this USB.' in output.getvalue(), flashed)
            self.assertEqual('./guaranine worker' in output.getvalue(), flashed)
            self.assertEqual('USB not flashed.' in output.getvalue(), not flashed)

    def test_ssh_hostkey_displays_fingerprint_and_requires_confirmation_before_pinning(self):
        blob = b'\0\0\0\x0bssh-ed25519\0\0\0\x20' + b'\x11' * 32
        raw = '100.64.0.8 ssh-ed25519 ' + base64.b64encode(blob).decode() + '\n'
        fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip('=')
        result = subprocess.CompletedProcess([], 0, stdout=raw)
        run = subprocess.run
        def key_command(arguments, **kwargs):
            return result if arguments[0] == 'ssh-keyscan' else run(arguments, **kwargs)
        for answer in ('yes', 'Y'):
            output = io.StringIO()
            with (patch.object(cli.subprocess, 'run', side_effect=key_command), patch('builtins.input', return_value=answer),
                  contextlib.redirect_stdout(output)):
                with cli.verified_connection('100.64.0.8', self.root) as (command, hosts):
                    self.assertIn('StrictHostKeyChecking=yes', command)
                    self.assertIn('GlobalKnownHostsFile=/dev/null', command)
                    self.assertEqual(hosts.read_text(), raw)
                    self.assertEqual(hosts.stat().st_mode & 0o777, 0o600)
                self.assertFalse(hosts.exists())
            self.assertIn(fingerprint, output.getvalue())
            self.assertIn('laptop screen or protected VM console', output.getvalue())
        for answer in ('', 'no', 'SHA256:WRONG'):
            with (patch.object(cli.subprocess, 'run', side_effect=key_command), patch('builtins.input', return_value=answer),
                  contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'identity not confirmed')):
                with cli.verified_connection('100.64.0.8', self.root): self.fail('Unconfirmed SSH key accepted')

    def test_data_disk_with_signatures_is_never_formatted(self):
        disk = {'path': '/dev/vdb', 'size': 64 * 2**30, 'model': 'Fixture', 'serial': 'disk-b', 'tran': 'virtio'}
        with (patch.object(cli, 'read_disks', return_value=[disk]), patch('builtins.input') as prompt,
              patch.object(cli.questionary, 'select') as select,
              patch.object(cli, 'private_command', side_effect=['', '{"signatures":[{"type":"xfs"}]}']) as command,
              contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError)):
            select.return_value.unsafe_ask.return_value = disk
            cli.initialize_data(['ssh'], self.root)
        prompt.assert_not_called()
        self.assertFalse(any('mkfs' in str(call) for call in command.call_args_list))

    def test_existing_data_label_is_reused_without_selection_or_formatting(self):
        with (patch.object(cli, 'private_command', return_value='present') as command,
              patch.object(cli.questionary, 'select') as select, patch('builtins.input') as prompt):
            cli.initialize_data(['ssh'], self.root)
        prompt.assert_not_called()
        select.assert_not_called()
        self.assertEqual(command.call_count, 1)

    def test_data_disk_selection_cancels_before_signature_checks_or_formatting(self):
        disk = {'path': '/dev/vdb', 'size': 64 * 2**30, 'model': 'Fixture', 'serial': 'disk-b', 'tran': 'virtio'}
        for answer in (False, None):
            with (self.subTest(answer=answer), patch.object(cli, 'read_disks', return_value=[disk]),
                  patch.object(cli, 'private_command', return_value='') as command,
                  patch.object(cli.questionary, 'select') as select, patch('builtins.input') as prompt,
                  contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'initialization cancelled')):
                select.return_value.unsafe_ask.return_value = answer
                cli.initialize_data(['ssh'], self.root)
            prompt.assert_not_called()
            self.assertEqual(command.call_count, 1)
            self.assertFalse(any('wipefs' in str(call) or 'mkfs' in str(call) for call in command.call_args_list))

    def test_data_disk_signatures_are_checked_again_after_confirmation(self):
        disk = {'path': '/dev/vdb', 'size': 64 * 2**30, 'model': 'Fixture', 'serial': 'disk-b', 'tran': 'virtio'}
        for signatures in ('[]', '[{"type":"ext4"}]'):
            replies = ['', '{"signatures":[]}', '{"signatures":' + signatures + '}', '']
            with (self.subTest(signatures=signatures), patch.object(cli, 'read_disks', return_value=[disk]),
                  patch.object(cli.questionary, 'select') as select,
                  patch('builtins.input', return_value='INITIALIZE /dev/vdb'),
                  patch.object(cli, 'private_command', side_effect=replies) as command,
                  contextlib.redirect_stdout(io.StringIO())):
                select.return_value.unsafe_ask.return_value = disk
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
                  patch.object(cli.questionary, 'select') as select,
                  patch.object(cli.subprocess, 'run') as run):
                cli.flash_iso(self.image)
                disks.assert_not_called()
                prompt.assert_not_called()
                select.assert_not_called()
                run.assert_not_called()

    def test_skip_cancellation_and_no_devices_never_unmount_or_write(self):
        for answer in (False, None, EOFError(), KeyboardInterrupt()):
            with (self.subTest(answer=answer), patch.object(cli, 'usb_disks', return_value=[self.disk]),
                  patch.object(cli.questionary, 'select') as select, patch('builtins.input') as prompt,
                  patch.object(cli.subprocess, 'run') as run):
                if isinstance(answer, BaseException):
                    select.return_value.unsafe_ask.side_effect = answer
                else:
                    select.return_value.unsafe_ask.return_value = answer
                self.assertFalse(cli.flash_iso(self.image))
                prompt.assert_not_called()
                run.assert_not_called()
        with (patch.object(cli, 'usb_disks', return_value=[]), patch('builtins.input') as prompt,
              patch.object(cli.questionary, 'select') as select,
              patch.object(cli.subprocess, 'run') as run):
            cli.flash_iso(self.image)
        prompt.assert_not_called()
        select.assert_not_called()
        run.assert_not_called()

    def test_wrong_confirmation_explains_required_word_without_writing(self):
        with (patch.object(cli, 'usb_disks', return_value=[self.disk]),
              patch.object(cli.questionary, 'select') as select, patch('builtins.input', return_value='yes'),
              patch.object(cli.subprocess, 'run') as run):
            select.return_value.unsafe_ask.return_value = self.disk
            self.assertFalse(cli.flash_iso(self.image))
        run.assert_not_called()
        output = self.output.getvalue()
        self.assertIn('Confirmation must be ERASE.', output)
        self.assertIn('Nothing was written to /dev/sdy.', output)

    def test_usb_enter_skips_and_arrow_selects_the_disk_with_real_terminal_input(self):
        real_select = cli.questionary.select
        for keys, expected in [('\r', False), ('\x1b[B\r', self.disk)]:
            with self.subTest(keys=keys), create_pipe_input() as keyboard:
                keyboard.send_text(keys)
                selected = []
                def terminal_select(*args, **kwargs):
                    question = real_select(*args, **kwargs, input=keyboard, output=DummyOutput())
                    unsafe_ask = question.unsafe_ask
                    def answer():
                        value = unsafe_ask()
                        selected.append(value)
                        return value
                    question.unsafe_ask = answer
                    return question
                with (patch.object(cli, 'usb_disks', return_value=[self.disk]),
                      patch.object(cli.questionary, 'select', side_effect=terminal_select),
                      patch('builtins.input', return_value='') as prompt,
                      patch.object(cli.subprocess, 'run') as run):
                    self.assertFalse(cli.flash_iso(self.image))
                self.assertEqual(len(selected), 1)
                self.assertIs(selected[0], expected)
                self.assertEqual(prompt.call_count, int(bool(expected)))
                run.assert_not_called()

    def test_only_the_confirmed_usb_is_unmounted_and_written_as_a_whole_disk(self):
        second = copy.deepcopy(self.disk)
        second.update(path='/dev/sdz', serial='DISPOSABLE-USB-B')
        second['children'][0]['path'] = '/dev/sdz1'
        unmounted = copy.deepcopy(second)
        unmounted['children'][0]['mountpoints'] = []
        with (patch.object(cli, 'usb_disks', side_effect=[[self.disk, second], [self.disk, second], [unmounted]]),
              patch.object(cli.questionary, 'select') as select,
              patch('builtins.input', return_value='ERASE'), patch.object(cli.subprocess, 'run') as run):
            select.return_value.unsafe_ask.return_value = second
            self.assertTrue(cli.flash_iso(self.image))
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ['sudo', '--', 'umount', '--all-targets', '--', '/dev/sdz1'],
            ['sudo', '--', 'dd', f'if={self.image}', 'of=/dev/sdz', 'bs=4M', 'oflag=dsync',
             'status=progress', 'conv=fsync,nocreat']])
        choices = select.call_args.kwargs['choices']
        self.assertIs(choices[2].value, second)
        self.assertIn('DISPOSABLE-USB-B', choices[2].title)
        self.assertIn('Writing ', self.output.getvalue())
        self.assertIn('Do not remove the USB.', self.output.getvalue())
        self.assertIn('Flashed guaranine-kairos.iso to /dev/sdz.', self.output.getvalue())

    def test_device_changes_before_or_after_unmount_never_start_a_write(self):
        changed = {**self.disk, 'serial': 'DIFFERENT-USB'}
        for reports in ([[self.disk], [changed]], [[self.disk], [self.disk], [changed]],
                        [[self.disk], [self.disk], [self.disk]]):
            with (self.subTest(reports=reports), patch.object(cli, 'usb_disks', side_effect=reports),
                  patch.object(cli.questionary, 'select') as select, patch('builtins.input', return_value='ERASE'),
                  patch.object(cli.subprocess, 'run') as run, self.assertRaises(ValueError)):
                select.return_value.unsafe_ask.return_value = self.disk
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
                  patch.object(cli.questionary, 'select') as select, patch('builtins.input', return_value='ERASE'),
                  patch.object(cli.subprocess, 'run', side_effect=effects) as run,
                  self.assertRaises(subprocess.CalledProcessError if fail_unmount else ValueError)):
                select.return_value.unsafe_ask.return_value = self.disk
                cli.flash_iso(self.image)
            self.assertEqual(run.call_count, 1 if fail_unmount else 2)
        self.assertNotIn('Flashed ', self.output.getvalue())

    def test_flash_logs_preserve_output_permissions_and_failure_details(self):
        unmounted = copy.deepcopy(self.disk)
        unmounted['children'][0]['mountpoints'] = []
        real_run = subprocess.run
        for outcome in ('success', 'write-error', 'timeout'):
            with self.subTest(outcome=outcome), tempfile.TemporaryFile(mode='w+') as terminal:
                self.output.seek(0)
                self.output.truncate()
                previous_logs = set(self.image.parent.glob('flash-*.log'))
                def write(arguments, **kwargs):
                    self.assertIn('dd', arguments)
                    script = ('import os,sys; os.write(1,b"DISPOSABLE stdout\\n"); '
                              'os.write(2,b"\\r12582912 bytes copied\\n"); '
                              'sys.exit(' + ('1' if outcome == 'write-error' else '0') + ')')
                    result = real_run([sys.executable, '-c', script], **kwargs)
                    if outcome == 'timeout':
                        raise subprocess.TimeoutExpired(arguments, 900)
                    return result
                with (patch.object(cli, 'usb_disks', return_value=[unmounted]),
                      patch.object(cli.questionary, 'select') as select, patch('builtins.input', return_value='ERASE'),
                      patch.object(cli.subprocess, 'run', side_effect=write),
                      contextlib.redirect_stderr(terminal)):
                    select.return_value.unsafe_ask.return_value = unmounted
                    if outcome == 'success':
                        self.assertTrue(cli.flash_iso(self.image))
                    else:
                        with self.assertRaisesRegex(ValueError, 'USB write failed.*Private log:') as error:
                            cli.flash_iso(self.image)
                log, = set(self.image.parent.glob('flash-*.log')) - previous_logs
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                self.assertIn(str(log), self.output.getvalue())
                terminal.seek(0)
                visible = terminal.read()
                for message in ('DISPOSABLE stdout', '12582912 bytes copied'):
                    self.assertIn(message, visible)
                    self.assertIn(message, log.read_text())
                self.assertIn(b'\r12582912', log.read_bytes())
                if outcome != 'success':
                    self.assertIn(str(log), str(error.exception))
                    self.assertIn('CalledProcessError' if outcome == 'write-error' else 'TimeoutExpired',
                                  log.read_text())
                self.assertEqual('Flashed ' in self.output.getvalue(), outcome == 'success')
                self.assertEqual(self.image.read_text(), 'DISPOSABLE-ISO')
