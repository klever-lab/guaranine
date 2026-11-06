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
        self.args = SimpleNamespace(role='worker', name='laptop-01', disk='/dev/vda',
                                    confirm_install_disk='/dev/vda', etcd_disk_selector=None)

    def test_one_command_uses_saved_settings_and_only_embeds_the_machine_profile(self):
        with (patch.object(cli, 'mint') as mint,
              patch.object(cli, 'private_command'),
              patch.object(cli, 'generate', return_value=self.root / 'nodes/laptop-01/config/machine.yaml') as generate,
              patch.object(cli, 'private_image', return_value=self.root / 'nodes/laptop-01/image/talos.iso') as image):
            cli.main(['worker', 'laptop-01', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda'])
        self.assertEqual(mint.call_args.args[:3], ('DISPOSABLE-OPERATOR-TOKEN', 'example.invalid', 'worker'))
        profile = generate.call_args.args[0]
        self.assertEqual(profile.endpoint, 'https://100.64.0.7:6443')
        self.assertEqual(profile.secrets, self.root / 'secrets.yaml')
        self.assertFalse(profile.bootstrap_controlplane)
        self.assertEqual(image.call_args.args[0].config, self.root / 'nodes/laptop-01/config/machine.yaml')
        self.assertEqual(image.call_args.args[0].format, 'iso')
        self.assertEqual((self.root / 'nodes/laptop-01').stat().st_mode & 0o777, 0o700)

    def test_bad_settings_disk_endpoint_or_existing_node_never_issue_a_key(self):
        with (patch.object(cli, 'mint') as mint, patch.object(cli, 'private_command')):
            for field, value in (('name', '../outside'), ('disk', '/dev/vda1'), ('confirm_install_disk', '/dev/vdb')):
                with self.subTest(field=field):
                    original = getattr(self.args, field)
                    setattr(self.args, field, value)
                    with self.assertRaises(ValueError): cli.prepare(self.args)
                    setattr(self.args, field, original)
            original = (self.root / 'operator.yaml').read_text()
            for content in ('tailnet: REPLACE_WITH_TAILNET\n', original.replace('100.64.0.7', '192.168.1.2'),
                            original.replace('DISPOSABLE-OPERATOR-TOKEN', 'INVALID MULTILINE TOKEN')):
                (self.root / 'operator.yaml').write_text(content)
                with self.assertRaises(ValueError): cli.prepare(self.args)
            (self.root / 'operator.yaml').write_text(original)
            cli.output_directory(self.root / 'nodes/laptop-01')
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
            cli.main(['worker', 'laptop-01', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda'])
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

    def test_native_cli_passes_arguments_to_the_verified_binary_unchanged(self):
        with patch.object(cli, 'talosctl', return_value='/verified/talosctl'), patch.object(cli.os, 'execv') as execute:
            cli.main(['talosctl', '--talosconfig', '/private/talosconfig', 'version', '--client'])
        execute.assert_called_once_with('/verified/talosctl',
            ['/verified/talosctl', '--talosconfig', '/private/talosconfig', 'version', '--client'])


if __name__ == '__main__':
    unittest.main()
