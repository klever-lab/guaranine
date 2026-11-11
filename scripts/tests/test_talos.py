"""Private machine preparation and security boundaries around upstream Talos tooling."""
from datetime import datetime, timedelta, timezone
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'scripts'))
import guaranine as cli

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


class TalosConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.key = self.root/'key.json'
        self.record = {'id': 'fixture', 'key': 'tskey-auth-DISPOSABLE-FIXTURE',
                       'created': NOW.isoformat(), 'expires': (NOW+timedelta(hours=24)).isoformat(),
                       'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': False,
                                                            'preauthorized': True, 'tags': ['tag:guaranine-worker']}}}}
        self.write()

    def write(self):
        self.key.write_text(json.dumps(self.record)); self.key.chmod(0o600)

    def test_tailnet_endpoint_and_explicit_single_control_plane_bootstrap(self):
        self.assertEqual(cli.endpoint('https://100.64.0.1:6443/', 'worker'), 'https://100.64.0.1:6443')
        self.assertEqual(cli.endpoint('https://127.0.0.1:6443', 'controlplane', True), 'https://127.0.0.1:6443')
        for value in ('http://100.64.0.1:6443', 'https://192.168.1.2:6443', 'https://example.com:6443',
                      'https://100.64.0.1:443', 'https://user:pass@100.64.0.1:6443', 'https://100.64.0.1:6443/?key=x',
                      'https://127.0.0.1:6443'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cli.endpoint(value, 'worker', True)

    def test_expiry_is_not_a_local_os_expiration_or_a_reusable_key(self):
        self.assertEqual(cli.auth_record(self.key, NOW)['id'], 'fixture')
        for field, value in [('reusable', True), ('ephemeral', True), ('preauthorized', False)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                saved = self.record['capabilities']['devices']['create'][field]
                self.record['capabilities']['devices']['create'][field] = value
                self.write()
                try:
                    cli.auth_record(self.key, NOW)
                finally:
                    self.record['capabilities']['devices']['create'][field] = saved
        self.record['expires'] = (NOW+timedelta(hours=25)).isoformat(); self.write()
        with self.assertRaises(ValueError): cli.auth_record(self.key, NOW)
        self.record['expires'] = (NOW+timedelta(hours=24)).isoformat(); self.write()
        with self.assertRaises(ValueError): cli.auth_record(self.key, NOW+timedelta(hours=24))

    def test_world_readable_symlink_and_in_checkout_inputs_are_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaises(ValueError): cli.protected(self.key)
        self.key.chmod(0o600)
        linked = self.root/'link'; linked.symlink_to(self.key)
        with self.assertRaises(ValueError): cli.protected(linked)
        with self.assertRaises(ValueError): cli.external(ROOT/'secrets.yaml')
        with self.assertRaises(FileExistsError): cli.output_directory(self.root)

    def test_private_profile_is_embedded_only_in_local_imager_input(self):
        secret = 'DISPOSABLE PRIVATE CONFIG CONTENT'
        for image_format in ('iso', 'azure'):
            profile = cli.imager_profile(secret, image_format)
            self.assertEqual(profile['customization']['embeddedMachineConfiguration'], secret)
            self.assertEqual(profile['input']['systemExtensions'][0]['imageRef'], cli.PINS['tailscale_extension'])
            self.assertNotIn('latest', json.dumps(profile))
            self.assertEqual(profile['platform'], 'azure' if image_format == 'azure' else 'metal')

    def test_shared_boot_profile_has_no_install_disk_cluster_authority_or_fixed_identity(self):
        boot = cli.boot_config('tskey-auth-DISPOSABLE-SHARED')
        docs = list(cli.yaml.safe_load_all(boot))
        self.assertEqual({doc['kind'] for doc in docs},
                         {'HostnameConfig', 'ExtensionServiceConfig', 'NetworkDefaultActionConfig', 'NetworkRuleConfig'})
        self.assertEqual(next(doc for doc in docs if doc['kind'] == 'HostnameConfig')['auto'], 'stable')
        self.assertNotIn('TS_HOSTNAME', boot)
        self.assertNotIn('disk', boot)
        self.assertNotIn('cluster', boot)
        rule = next(doc for doc in docs if doc['kind'] == 'NetworkRuleConfig')
        self.assertEqual(rule['portSelector'], {'ports': [50000], 'protocol': 'tcp'})
        self.assertEqual(rule['ingress'], [{'subnet': cli.TAILNET}])
        profile = cli.imager_profile(boot, 'iso', maintenance=True)
        self.assertEqual(profile['input']['systemExtensions'], [{'tarballPath': '/out/tailscale-maintenance.tar'}])

    def test_only_an_explicit_batch_accepts_a_reusable_key(self):
        self.record['capabilities']['devices']['create']['reusable'] = True
        self.write()
        self.assertEqual(cli.auth_record(self.key, NOW, reusable=True)['id'], 'fixture')
        with self.assertRaises(ValueError): cli.auth_record(self.key, NOW)
        with self.assertRaises(ValueError): cli.auth_record(self.key, NOW + timedelta(hours=24), reusable=True)

    def test_corrupt_download_never_replaces_the_destination(self):
        import io
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        target = self.root/'image.iso'
        with (patch.object(cli.urllib.request, 'urlopen', side_effect=lambda *a, **kw: Response(b'CORRUPT')),
              patch.object(cli.time, 'sleep')):
            with self.assertRaises(ValueError):
                cli.fetch({'url': 'https://invalid.test/image', 'sha256': hashlib.sha256(b'EXPECTED').hexdigest()}, target)
        self.assertFalse(target.exists())
        self.assertEqual(set(self.root.iterdir()), {self.key})

    def test_private_command_errors_stay_in_protected_files(self):
        marker = 'DISPOSABLE-SECRET-DO-NOT-PRINT'
        command = [sys.executable, '-c', 'import sys; print(sys.stdin.read()); raise SystemExit(1)']
        with self.assertRaises(ValueError) as error:
            cli.private_command(command, self.root, data=marker)
        self.assertNotIn(marker, str(error.exception))
        logs = list(self.root.glob('error-*.log'))
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
        self.assertIn(marker, logs[0].read_text())


class MachineCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, GUARANINE_OPERATOR_DIR=str(self.root))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        cli.private_write(self.root / 'operator.yaml',
                             'tailnet: example.invalid\ntailscale_api_token: DISPOSABLE-OPERATOR-TOKEN\nendpoint: https://100.64.0.7:6443\n')
        cli.private_write(self.root / 'secrets.yaml', 'DISPOSABLE-CLUSTER-SECRETS')
        self.args = SimpleNamespace(role='controlplane', name='guaranine-controlplane', disk='/dev/vda',
                                    confirm_install_disk='/dev/vda', etcd_disk_selector='disk.dev_path == "/dev/vdb"')

    def test_one_iso_command_embeds_networking_and_a_batch_key_without_reading_cluster_secrets(self):
        (self.root / 'secrets.yaml').unlink()

        def create_key(token, tailnet, role, output, *, reusable=False):
            now = datetime.now(timezone.utc)
            cli.private_write(output, json.dumps({
                'id': 'batch', 'key': 'tskey-auth-DISPOSABLE-BATCH', 'created': now.isoformat(),
                'expires': (now + timedelta(hours=24)).isoformat(),
                'capabilities': {'devices': {'create': {'reusable': reusable, 'ephemeral': False,
                                 'preauthorized': True, 'tags': ['tag:guaranine-' + role]}}}}))

        with (patch.object(cli, 'mint', side_effect=create_key) as mint,
              patch.object(cli, 'private_command') as docker,
              patch.object(cli, 'generate') as generate,
              patch.object(cli, 'private_image', return_value=self.root / 'installer/image/talos.iso') as image,
              contextlib.redirect_stdout(io.StringIO())):
            cli.main(['iso'])
        self.assertEqual(mint.call_args.args[:3], ('DISPOSABLE-OPERATOR-TOKEN', 'example.invalid', 'worker'))
        self.assertEqual(mint.call_args.kwargs, {'reusable': True})
        docker.assert_called_once_with(['sudo', '--', 'docker', 'info', '--format', '{{.OSType}}'], self.root)
        generate.assert_not_called()
        self.assertEqual(image.call_args.args[0].config, self.root / 'installer/boot.yaml')
        self.assertEqual(image.call_args.args[0].format, 'iso')
        self.assertEqual(image.call_args.kwargs, {'use_sudo': True, 'maintenance': True})
        self.assertNotIn('DISPOSABLE-OPERATOR-TOKEN', (self.root / 'installer/boot.yaml').read_text())
        self.assertEqual((self.root / 'installer').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'installer/boot.yaml').stat().st_mode & 0o777, 0o600)

    def test_bad_settings_disk_endpoint_or_existing_node_never_issue_a_key(self):
        with (patch.object(cli, 'mint') as mint, patch.object(cli, 'private_command')):
            for field, value in (('name', '../outside'), ('disk', '/dev/vda1'), ('confirm_install_disk', '/dev/vdb')):
                with self.subTest(field=field):
                    original = getattr(self.args, field)
                    setattr(self.args, field, value)
                    with self.assertRaises(ValueError): cli.prepare(self.args)
                    setattr(self.args, field, original)
            original = (self.root / 'operator.yaml').read_text()
            for content in ('tailnet: REPLACE_WITH_TAILNET\n',
                            original.replace('DISPOSABLE-OPERATOR-TOKEN', 'INVALID MULTILINE TOKEN')):
                (self.root / 'operator.yaml').write_text(content)
                with self.assertRaises(ValueError): cli.prepare(self.args)
            (self.root / 'operator.yaml').write_text(original)
            cli.output_directory(self.root / 'nodes/guaranine-controlplane')
            with self.assertRaises(ValueError): cli.prepare(self.args)
            mint.assert_not_called()

    def test_docker_failure_does_not_create_key_or_partial_node_output(self):
        with (patch.object(cli, 'mint') as mint,
              patch.object(cli, 'private_command', side_effect=ValueError('Docker unavailable'))):
            with self.assertRaisesRegex(ValueError, 'Docker unavailable'): cli.prepare(self.args)
            mint.assert_not_called()
        self.assertFalse((self.root / 'nodes').exists())

    def test_malformed_private_settings_never_echo_the_api_credential(self):
        marker = 'DISPOSABLE-SECRET-NEVER-PRINT'
        (self.root / 'operator.yaml').write_text('tailscale_api_token: [' + marker)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            cli.main(['iso'])
        self.assertIn('Invalid private YAML', errors.getvalue())
        self.assertNotIn(marker, errors.getvalue())

    def test_controlplane_requires_an_explicit_retained_disk_and_uses_azure_format(self):
        with (patch.object(cli, 'mint') as mint,
              patch.object(cli, 'private_command'), patch.object(cli, 'generate') as generate,
              patch.object(cli, 'private_image', return_value=self.root / 'controlplane.vhd.zst') as image):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(['controlplane', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda'])
            mint.assert_not_called()
            cli.main(['controlplane', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda',
                         '--etcd-disk-selector', 'disk.dev_path == "/dev/vdb"'])
        self.assertEqual(generate.call_args.args[0].endpoint, 'https://127.0.0.1:6443')
        self.assertEqual(generate.call_args.args[0].etcd_disk_selector, 'disk.dev_path == "/dev/vdb"')
        self.assertEqual(image.call_args.args[0].format, 'azure')
        self.assertTrue(image.call_args.kwargs['use_sudo'])

    def test_native_cli_passes_arguments_to_the_verified_binary_unchanged(self):
        with patch.object(cli, 'talosctl', return_value='/verified/talosctl'), patch.object(cli.os, 'execv') as execute:
            cli.main(['talosctl', '--talosconfig', '/private/talosconfig', 'version', '--client'])
        execute.assert_called_once_with('/verified/talosctl',
            ['/verified/talosctl', '--talosconfig', '/private/talosconfig', 'version', '--client'])


class WorkerInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        environment = patch.dict(os.environ, GUARANINE_OPERATOR_DIR=str(self.root))
        environment.start()
        self.addCleanup(environment.stop)
        cli.private_write(self.root / 'operator.yaml', 'tailnet: example.invalid\ntailscale_api_token: DISPOSABLE-OPERATOR\n'
                          'endpoint: https://100.64.0.7:6443\n')
        cli.private_write(self.root / 'secrets.yaml', 'DISPOSABLE-CLUSTER-SECRETS')
        cli.output_directory(self.root / 'installer')
        now = datetime.now(timezone.utc)
        self.key = {'key': 'tskey-auth-DISPOSABLE-BATCH', 'created': now.isoformat(),
                    'expires': (now + timedelta(hours=24)).isoformat(),
                    'capabilities': {'devices': {'create': {'reusable': True, 'ephemeral': False,
                                     'preauthorized': True, 'tags': ['tag:guaranine-worker']}}}}
        cli.private_write(self.root / 'installer/tailscale-key.json', json.dumps(self.key))
        self.disk = {'dev_path': '/dev/nvme0n1', 'size': 512000000000, 'pretty_size': '512 GB',
                     'readonly': False, 'cdrom': False, 'model': 'Fixture SSD', 'serial': 'DISPOSABLE-DISK-1', 'transport': 'nvme'}
        self.resources = [{'spec': self.disk},
                          {'spec': {**self.disk, 'dev_path': '/dev/sda', 'model': 'Fixture USB', 'transport': 'usb'}},
                          {'spec': {**self.disk, 'dev_path': '/dev/sr0', 'cdrom': True}},
                          {'spec': {**self.disk, 'dev_path': '/dev/loop0', 'readonly': True}},
                          {'spec': {**self.disk, 'dev_path': '/dev/nvme0n1p1'}}]
        self.raw = '\n'.join(json.dumps(resource, indent=2) for resource in self.resources)
        self.answers = ['100.64.0.8', 'SHA256:DISPOSABLE-FINGERPRINT', 'garage-01', '1', 'ERASE garage-01 /dev/nvme0n1']

    def test_name_and_discovered_disk_are_applied_together_with_pinned_peer_certificate(self):
        calls = []

        def command(args, directory, **kwargs):
            calls.append(args)
            return self.raw if 'get' in args else ''

        console = io.StringIO()
        with (patch('builtins.input', side_effect=self.answers), patch.object(cli, 'private_command', side_effect=command),
              patch.object(cli, 'talosctl', return_value='/verified/talosctl'), patch.object(cli, 'mint') as mint,
              patch.object(cli, 'private_image') as image, patch.object(cli, 'generate', return_value=self.root / 'worker.yaml') as generate,
              contextlib.redirect_stdout(console)):
            cli.main(['worker'])
        profile = generate.call_args.args[0]
        self.assertEqual(profile.name, 'garage-01')
        self.assertEqual(profile.disk, '/dev/nvme0n1')
        self.assertEqual(profile.confirm_install_disk, profile.disk)
        self.assertEqual(profile.endpoint, 'https://100.64.0.7:6443')
        self.assertEqual(profile.tailscale_key, self.root / 'installer/tailscale-key.json')
        self.assertEqual(generate.call_args.kwargs, {'reusable': True})
        self.assertEqual(calls[-1], ['/verified/talosctl', 'apply-config', '--insecure', '--nodes', '100.64.0.8',
                                    '--cert-fingerprint', self.answers[1], '--file', str(self.root / 'worker.yaml')])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all('--cert-fingerprint' in command for command in calls))
        mint.assert_not_called()
        image.assert_not_called()
        self.assertIn('Fixture SSD', console.getvalue())
        self.assertIn('Fixture USB', console.getvalue())
        self.assertNotIn('/dev/loop0', console.getvalue())
        self.assertNotIn('DISPOSABLE-CLUSTER-SECRETS', console.getvalue())
        self.assertNotIn('tskey-auth-', console.getvalue())

    def test_no_default_disk_cancellation_and_changed_disk_never_apply_or_generate_configuration(self):
        for answers, reports in [
            (self.answers[:3] + [''], [self.raw]),
            (self.answers[:3] + ['3'], [self.raw]),
            (self.answers[:-1] + ['no'], [self.raw]),
            (self.answers, [self.raw, json.dumps({'spec': {**self.disk, 'serial': 'DIFFERENT-DISK'}})]),
        ]:
            with (self.subTest(answers=answers), patch('builtins.input', side_effect=answers),
                  patch.object(cli, 'private_command', side_effect=reports) as command,
                  patch.object(cli, 'talosctl', return_value='/verified/talosctl'), patch.object(cli, 'generate') as generate,
                  contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
                  self.assertRaises(SystemExit)):
                cli.main(['worker'])
            generate.assert_not_called()
            self.assertTrue(all('apply-config' not in call.args[0] for call in command.call_args_list))
            self.assertFalse((self.root / 'nodes').exists())

    def test_invalid_names_addresses_and_missing_fingerprint_fail_before_querying_a_machine(self):
        for answers in [['192.168.1.4'], ['100.64.0.8', ''], self.answers[:2] + ['../outside'],
                        self.answers[:2] + ['HAS SPACES']]:
            with (self.subTest(answers=answers), patch('builtins.input', side_effect=answers),
                  patch.object(cli, 'private_command') as command, contextlib.redirect_stdout(io.StringIO()),
                  contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
                cli.main(['worker'])
            command.assert_not_called()

    def test_batch_expiring_during_profile_generation_never_sends_installation_configuration(self):
        def expired_during_generation(*args, **kwargs):
            now = datetime.now(timezone.utc)
            self.key['created'] = (now - timedelta(hours=1)).isoformat()
            self.key['expires'] = (now - timedelta(seconds=1)).isoformat()
            (self.root / 'installer/tailscale-key.json').write_text(json.dumps(self.key))
            return self.root / 'worker.yaml'

        with (patch('builtins.input', side_effect=self.answers), patch.object(cli, 'private_command', return_value=self.raw) as command,
              patch.object(cli, 'talosctl', return_value='/verified/talosctl'),
              patch.object(cli, 'generate', side_effect=expired_during_generation),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
            cli.main(['worker'])
        self.assertTrue(all('apply-config' not in call.args[0] for call in command.call_args_list))

    def test_expired_batch_and_existing_worker_name_never_modify_the_laptop(self):
        cli.output_directory(self.root / 'nodes/garage-01')
        with (patch('builtins.input', side_effect=self.answers[:3]), patch.object(cli, 'private_command') as command,
              contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
            cli.main(['worker'])
        command.assert_not_called()
        self.key['created'] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.key['expires'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        (self.root / 'installer/tailscale-key.json').write_text(json.dumps(self.key))
        with (patch('builtins.input') as prompt, patch.object(cli, 'private_command') as command,
              contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
            cli.main(['worker'])
        prompt.assert_not_called()
        command.assert_not_called()


class MaintenanceExtensionTests(unittest.TestCase):
    def test_maintenance_uses_ram_state_without_cri_and_preserves_the_pinned_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'scratch'
            directory.mkdir()
            service = {'name': 'tailscale', 'depends': [{'service': 'cri'}, {'configuration': True}],
                       'container': {'entrypoint': '/usr/local/bin/containerboot',
                                     'environment': ['TS_STATE_DIR=/var/lib/tailscale'],
                                     'mounts': [{'source': '/var/lib/tailscale', 'destination': '/var/lib/tailscale'},
                                                {'source': '/var/run/tailscale', 'destination': '/var/run/tailscale'}]}}
            original = directory / 'fixture.tar'
            with tarfile.open(original, 'w') as archive:
                for name, data in [('rootfs/usr/local/etc/containers/tailscale.yaml', cli.yaml.safe_dump(service).encode()),
                                   ('rootfs/usr/local/bin/containerboot', b'PINNED UPSTREAM BINARY')]:
                    member = tarfile.TarInfo(name); member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))

            def export(command, **kwargs):
                self.assertEqual(command, ['docker', 'export', 'fixture-container'])
                kwargs['stdout'].write(original.read_bytes())
                return subprocess.CompletedProcess(command, 0)

            with (patch.object(cli, 'private_command', side_effect=['fixture-container\n', '']) as command,
                  patch.object(cli.subprocess, 'run', side_effect=export)):
                cli.maintenance_extension(directory, ['docker'])
            self.assertEqual(command.call_args_list[0].args[0],
                             ['docker', 'create', '--platform', 'linux/amd64', '--entrypoint', '/unused', cli.PINS['tailscale_extension']])
            self.assertEqual(command.call_args_list[1].args[0], ['docker', 'rm', 'fixture-container'])
            with tarfile.open(directory / 'tailscale-maintenance.tar') as archive:
                adjusted = cli.yaml.safe_load(archive.extractfile('rootfs/usr/local/etc/containers/tailscale.yaml').read())
                self.assertEqual(adjusted['depends'], [{'configuration': True}])
                self.assertEqual(adjusted['container']['environment'], ['TS_STATE_DIR=/var/run/tailscale'])
                self.assertEqual(adjusted['container']['mounts'], [{'source': '/var/run/tailscale', 'destination': '/var/run/tailscale'}])
                adjusted['depends'].insert(0, {'service': 'cri'})
                adjusted['container']['environment'] = service['container']['environment']
                adjusted['container']['mounts'] = service['container']['mounts']
                self.assertEqual(adjusted, service)
                self.assertEqual(archive.extractfile('rootfs/usr/local/bin/containerboot').read(), b'PINNED UPSTREAM BINARY')
            self.assertFalse((directory / 'tailscale-upstream.tar').exists())


class PreparedControlPlaneBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.node = self.root / 'nodes/guaranine-controlplane'
        (self.node / 'config').mkdir(parents=True)
        self.config = self.node / 'config/machine.yaml'
        cli.private_write(self.config, 'version: v1alpha1\nmachine:\n  type: controlplane\n'
                          '---\napiVersion: v1alpha1\nkind: ExtensionServiceConfig\nname: tailscale\n'
                          'environment:\n  - TS_AUTHKEY=tskey-auth-DISPOSABLE-BUILD\n')
        self.record = {'key': 'tskey-auth-DISPOSABLE-BUILD',
                       'created': datetime.now(timezone.utc).isoformat(),
                       'expires': (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat(),
                       'capabilities': {'devices': {'create': {'reusable': False, 'ephemeral': False,
                                                              'preauthorized': True}}}}
        cli.private_write(self.node / 'tailscale-key.json', json.dumps(self.record))
        self.environment = patch.dict(os.environ, GUARANINE_OPERATOR_DIR=str(self.root))
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_named_command_elevates_only_docker_and_keeps_private_operator_owned_outputs(self):
        def imager(command, **kwargs):
            scratch = next(path for path in (self.node / 'image').glob('imager-*') if path.is_dir())
            self.assertTrue(scratch.is_dir())
            self.assertEqual(scratch.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(scratch.iterdir()), [])
            self.assertEqual(command, ['sudo', '--', 'docker', 'run', '--rm', '--privileged',
                                       '--platform', 'linux/amd64', '-i', '--mount',
                                       f'type=bind,source={scratch},target=/out', cli.PINS['imager'], '-', '--tar-to-stdout'])
            self.assertIn(b'tskey-auth-DISPOSABLE-BUILD', kwargs['input'])
            self.assertNotIn('tskey-auth-DISPOSABLE-BUILD', repr(command))
            # The native imager leaves output files in /out; only the streamed
            # archive is promoted to operator-owned final files.
            staged = scratch / 'azure-amd64.vhd.zst'
            staged.write_bytes(b'DISPOSABLE IMAGE FIXTURE')
            staged.chmod(0o400)
            with tarfile.open(fileobj=kwargs['stdout'], mode='w') as archive:
                member = tarfile.TarInfo('azure-amd64.vhd.zst')
                content = b'DISPOSABLE IMAGE FIXTURE'
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            return subprocess.CompletedProcess(command, 0)

        with (patch.object(cli, 'mint') as mint, patch.object(cli, 'talosctl', return_value='/verified/talosctl'),
              patch.object(cli, 'private_command') as validate, patch.object(cli.subprocess, 'run', side_effect=imager),
              contextlib.redirect_stdout(io.StringIO())):
            cli.main(['build-controlplane'])
        mint.assert_not_called()
        validate.assert_called_once_with(['/verified/talosctl', 'validate', '--strict', '--mode', 'metal',
                                          '--config', str(self.config)], self.node / 'image')
        output = self.node / 'image/azure-amd64.vhd.zst'
        self.assertEqual(output.read_bytes(), b'DISPOSABLE IMAGE FIXTURE')
        self.assertEqual(output.stat().st_uid, os.geteuid())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse((output.parent / 'imager-output.tar').exists())
        self.assertEqual((output.parent / 'imager.log').stat().st_mode & 0o777, 0o600)
        self.assertEqual({path.name for path in output.parent.iterdir()}, {'azure-amd64.vhd.zst', 'imager.log'})

    def test_expired_key_or_existing_output_never_starts_sudo_or_mints_another_key(self):
        terminal = io.StringIO()
        with (patch.object(cli, 'mint') as mint, patch.object(cli.subprocess, 'run') as run,
              patch.object(cli, 'talosctl', return_value='/verified/talosctl'),
              contextlib.redirect_stderr(terminal), contextlib.redirect_stdout(terminal)):
            cli.output_directory(self.node / 'image')
            cli.private_write(self.node / 'image/imager.log', 'DISPOSABLE PRIVATE DIAGNOSTIC')
            with self.assertRaises(SystemExit):
                cli.main(['build-controlplane'])
            self.assertIn('Azure VM provisioning is separate', terminal.getvalue())
            self.assertNotIn('Building the private control-plane image', terminal.getvalue())
            self.assertNotIn('DISPOSABLE PRIVATE DIAGNOSTIC', terminal.getvalue())
            self.assertEqual((self.node / 'image/imager.log').read_text(), 'DISPOSABLE PRIVATE DIAGNOSTIC')
            (self.node / 'image/imager.log').unlink()
            (self.node / 'image').rmdir()
            now = datetime.now(timezone.utc)
            self.record['created'] = (now - timedelta(days=2)).isoformat()
            self.record['expires'] = (now - timedelta(days=1)).isoformat()
            (self.node / 'tailscale-key.json').write_text(json.dumps(self.record))
            with self.assertRaises(SystemExit):
                cli.main(['build-controlplane'])
            mint.assert_not_called()
            run.assert_not_called()
        self.assertFalse((self.node / 'image').exists())

    def test_sudo_failure_keeps_diagnostics_private_and_preserves_the_prepared_identity(self):
        before = self.config.read_bytes()

        def denied(command, **kwargs):
            scratch = next(path for path in (self.node / 'image').glob('imager-*') if path.is_dir())
            (scratch / 'partial.vhd').write_bytes(b'DISPOSABLE PARTIAL IMAGE')
            kwargs['stderr'].write(b'DISPOSABLE PRIVATE DIAGNOSTIC')
            return subprocess.CompletedProcess(command, 1)

        terminal = io.StringIO()
        with (patch.object(cli, 'talosctl', return_value='/verified/talosctl'),
              patch.object(cli, 'private_command'), patch.object(cli.subprocess, 'run', side_effect=denied),
              contextlib.redirect_stdout(terminal), contextlib.redirect_stderr(terminal),
              self.assertRaises(SystemExit)):
            cli.main(['build-controlplane'])
        self.assertNotIn('DISPOSABLE PRIVATE DIAGNOSTIC', terminal.getvalue())
        self.assertEqual(self.config.read_bytes(), before)
        log = self.node / 'image/imager.log'
        self.assertIn('DISPOSABLE PRIVATE DIAGNOSTIC', log.read_text())
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(log.parent.glob('imager-*/')))


if __name__ == '__main__':
    unittest.main()
