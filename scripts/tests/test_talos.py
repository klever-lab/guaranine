"""Security and publication boundaries around upstream Talos tooling."""
from datetime import datetime, timedelta, timezone
import contextlib
import copy
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
sys.path.insert(0, str(ROOT/'scripts/talos'))
import common
import config
import images

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
        self.assertEqual(config.endpoint('https://100.64.0.1:6443/', 'worker'), 'https://100.64.0.1:6443')
        self.assertEqual(config.endpoint('https://127.0.0.1:6443', 'controlplane', True), 'https://127.0.0.1:6443')
        for value in ('http://100.64.0.1:6443', 'https://192.168.1.2:6443', 'https://example.com:6443',
                      'https://100.64.0.1:443', 'https://user:pass@100.64.0.1:6443', 'https://100.64.0.1:6443/?key=x',
                      'https://127.0.0.1:6443'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.endpoint(value, 'worker', True)

    def test_expiry_is_not_a_local_os_expiration_or_a_reusable_key(self):
        self.assertEqual(config.auth_record(self.key, NOW)['id'], 'fixture')
        for field, value in [('reusable', True), ('ephemeral', True), ('preauthorized', False)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                saved = self.record['capabilities']['devices']['create'][field]
                self.record['capabilities']['devices']['create'][field] = value
                self.write()
                try:
                    config.auth_record(self.key, NOW)
                finally:
                    self.record['capabilities']['devices']['create'][field] = saved
        self.record['expires'] = (NOW+timedelta(hours=25)).isoformat(); self.write()
        with self.assertRaises(ValueError): config.auth_record(self.key, NOW)
        self.record['expires'] = (NOW+timedelta(hours=24)).isoformat(); self.write()
        with self.assertRaises(ValueError): config.auth_record(self.key, NOW+timedelta(hours=24))

    def test_world_readable_symlink_and_in_checkout_inputs_are_rejected(self):
        self.key.chmod(0o644)
        with self.assertRaises(ValueError): common.protected(self.key)
        self.key.chmod(0o600)
        linked = self.root/'link'; linked.symlink_to(self.key)
        with self.assertRaises(ValueError): common.protected(linked)
        with self.assertRaises(ValueError): common.external(ROOT/'secrets.yaml')
        with self.assertRaises(FileExistsError): common.output_directory(self.root)

    def test_private_profile_is_embedded_only_in_local_imager_input(self):
        secret = 'DISPOSABLE PRIVATE CONFIG CONTENT'
        for image_format in ('iso', 'azure'):
            profile = images.imager_profile(secret, image_format)
            self.assertEqual(profile['customization']['embeddedMachineConfiguration'], secret)
            self.assertEqual(profile['input']['systemExtensions'][0]['imageRef'], common.PINS['tailscale_extension'])
            self.assertNotIn('latest', json.dumps(profile))
            self.assertEqual(profile['platform'], 'azure' if image_format == 'azure' else 'metal')

    def test_corrupt_download_never_replaces_the_destination(self):
        import io
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        target = self.root/'image.iso'
        with (patch.object(common.urllib.request, 'urlopen', side_effect=lambda *a, **kw: Response(b'CORRUPT')),
              patch.object(common.time, 'sleep')):
            with self.assertRaises(ValueError):
                common.fetch({'url': 'https://invalid.test/image', 'sha256': hashlib.sha256(b'EXPECTED').hexdigest()}, target)
        self.assertFalse(target.exists())
        self.assertEqual(set(self.root.iterdir()), {self.key})

    def test_private_command_errors_stay_in_protected_files(self):
        marker = 'DISPOSABLE-SECRET-DO-NOT-PRINT'
        command = [sys.executable, '-c', 'import sys; print(sys.stdin.read()); raise SystemExit(1)']
        with self.assertRaises(ValueError) as error:
            common.private_command(command, self.root, data=marker)
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
        common.private_write(self.root / 'operator.yaml',
                             'tailnet: example.invalid\ntailscale_api_token: DISPOSABLE-OPERATOR-TOKEN\nendpoint: https://100.64.0.7:6443\n')
        common.private_write(self.root / 'secrets.yaml', 'DISPOSABLE-CLUSTER-SECRETS')
        self.args = SimpleNamespace(role='worker', name='laptop-01', disk='/dev/vda',
                                    confirm_install_disk='/dev/vda', etcd_disk_selector=None)

    def test_one_command_uses_saved_settings_and_only_embeds_the_machine_profile(self):
        with (patch.object(config, 'mint') as mint,
              patch.object(config, 'private_command'),
              patch.object(config, 'generate', return_value=self.root / 'nodes/laptop-01/config/machine.yaml') as generate,
              patch.object(config, 'private_image', return_value=self.root / 'nodes/laptop-01/image/talos.iso') as image):
            config.main(['worker', 'laptop-01', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda'])
        self.assertEqual(mint.call_args.args[:3], ('DISPOSABLE-OPERATOR-TOKEN', 'example.invalid', 'worker'))
        profile = generate.call_args.args[0]
        self.assertEqual(profile.endpoint, 'https://100.64.0.7:6443')
        self.assertEqual(profile.secrets, self.root / 'secrets.yaml')
        self.assertFalse(profile.bootstrap_controlplane)
        self.assertEqual(image.call_args.args[0].config, self.root / 'nodes/laptop-01/config/machine.yaml')
        self.assertEqual(image.call_args.args[0].format, 'iso')
        self.assertEqual((self.root / 'nodes/laptop-01').stat().st_mode & 0o777, 0o700)

    def test_bad_settings_disk_endpoint_or_existing_node_never_issue_a_key(self):
        with (patch.object(config, 'mint') as mint, patch.object(config, 'private_command')):
            for field, value in (('name', '../outside'), ('disk', '/dev/vda1'), ('confirm_install_disk', '/dev/vdb')):
                with self.subTest(field=field):
                    original = getattr(self.args, field)
                    setattr(self.args, field, value)
                    with self.assertRaises(ValueError): config.prepare(self.args)
                    setattr(self.args, field, original)
            original = (self.root / 'operator.yaml').read_text()
            for content in ('tailnet: REPLACE_WITH_TAILNET\n', original.replace('100.64.0.7', '192.168.1.2'),
                            original.replace('DISPOSABLE-OPERATOR-TOKEN', 'INVALID MULTILINE TOKEN')):
                (self.root / 'operator.yaml').write_text(content)
                with self.assertRaises(ValueError): config.prepare(self.args)
            (self.root / 'operator.yaml').write_text(original)
            common.output_directory(self.root / 'nodes/laptop-01')
            with self.assertRaises(ValueError): config.prepare(self.args)
            mint.assert_not_called()

    def test_docker_failure_does_not_create_key_or_partial_node_output(self):
        with (patch.object(config, 'mint') as mint,
              patch.object(config, 'private_command', side_effect=ValueError('Docker unavailable'))):
            with self.assertRaisesRegex(ValueError, 'Docker unavailable'): config.prepare(self.args)
            mint.assert_not_called()
        self.assertFalse((self.root / 'nodes').exists())

    def test_malformed_private_settings_never_echo_the_api_credential(self):
        marker = 'DISPOSABLE-SECRET-NEVER-PRINT'
        (self.root / 'operator.yaml').write_text('tailscale_api_token: [' + marker)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            config.main(['worker', 'laptop-01', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda'])
        self.assertIn('Invalid private YAML', errors.getvalue())
        self.assertNotIn(marker, errors.getvalue())

    def test_controlplane_requires_an_explicit_retained_disk_and_uses_azure_format(self):
        with (patch.object(config, 'mint') as mint,
              patch.object(config, 'private_command'), patch.object(config, 'generate') as generate,
              patch.object(config, 'private_image', return_value=self.root / 'controlplane.vhd.zst') as image):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                config.main(['controlplane', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda'])
            mint.assert_not_called()
            config.main(['controlplane', '--disk', '/dev/vda', '--confirm-install-disk', '/dev/vda',
                         '--etcd-disk-selector', 'disk.dev_path == "/dev/vdb"'])
        self.assertEqual(generate.call_args.args[0].endpoint, 'https://127.0.0.1:6443')
        self.assertEqual(generate.call_args.args[0].etcd_disk_selector, 'disk.dev_path == "/dev/vdb"')
        self.assertEqual(image.call_args.args[0].format, 'azure')

    def test_native_cli_passes_arguments_to_the_verified_binary_unchanged(self):
        with patch.object(config, 'talosctl', return_value='/verified/talosctl'), patch.object(config.os, 'execv') as execute:
            config.main(['talosctl', '--talosconfig', '/private/talosconfig', 'version', '--client'])
        execute.assert_called_once_with('/verified/talosctl',
            ['/verified/talosctl', '--talosconfig', '/private/talosconfig', 'version', '--client'])


class TalosReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root/images.ISO).write_bytes(b'DISPOSABLE IMAGE FIXTURE')
        self.sha = common.digest(self.root/images.ISO)
        self.pin = patch.dict(common.PINS['iso'], {'sha256': self.sha}); self.pin.start(); self.addCleanup(self.pin.stop)
        self.manifest = {'os': 'talos', 'private': False, 'source_commit': 'a'*40, 'source_dirty': False,
                         'iso_sha256': self.sha, 'talos_version': common.PINS['talos']}
        boot = {'boot': 'passed', 'maintenance_api': 'passed', 'disk_unchanged': 'passed', 'talos_version': common.PINS['talos']}
        self.evidence = {'source_commit': 'a'*40, 'iso_sha256': self.sha,
                         'firmware': {'bios': copy.deepcopy(boot), 'uefi': copy.deepcopy(boot)}}
        self.write()

    def write(self):
        (self.root/'build.json').write_text(json.dumps(self.manifest))
        (self.root/'qemu-results.json').write_text(json.dumps(self.evidence))

    def test_finalization_binds_both_boot_modes_to_public_image_and_source(self):
        manifest = images.finalize(self.root)
        self.assertEqual(manifest['tests'], self.evidence)
        for line in (self.root/'SHA256SUMS').read_text().splitlines():
            expected, name = line.split()
            self.assertEqual(expected, common.digest(self.root/name))

    def test_private_foreign_failed_and_skipped_evidence_cannot_publish(self):
        for mode in ('bios', 'uefi'):
            for field in ('boot', 'maintenance_api', 'disk_unchanged'):
                for bad in ('failed', 'skipped', None):
                    with self.subTest(mode=mode, field=field, bad=bad):
                        saved = self.evidence['firmware'][mode][field]
                        self.evidence['firmware'][mode][field] = bad; self.write()
                        with self.assertRaises(ValueError): images.finalize(self.root)
                        self.evidence['firmware'][mode][field] = saved
        self.evidence['source_commit'] = 'b'*40; self.write()
        with self.assertRaises(ValueError): images.finalize(self.root)
        self.evidence['source_commit'] = 'a'*40
        self.manifest['private'] = True; self.write()
        with self.assertRaises(ValueError): images.finalize(self.root)
        self.manifest['private'] = False; self.write()
        (self.root/'private.json').write_text('{}')
        with self.assertRaises(ValueError): images.finalize(self.root)


if __name__ == '__main__':
    unittest.main()
