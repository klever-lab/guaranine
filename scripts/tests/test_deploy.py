"""Deployment refusal and the boundary between CI enrollment and cluster secrets."""
from datetime import datetime, timedelta, timezone
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import guaranine as cli

spec = importlib.util.spec_from_file_location('deployment', ROOT / 'scripts/deploy-controller.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class DeploymentTests(unittest.TestCase):
    def test_plan_refuses_deletion_replacement_forgetting_and_live_controller_updates(self):
        for address, actions, had_controller in [
            ('openstack_images_image_v2.controller', ['delete'], False),
            ('openstack_compute_instance_v2.controller', ['delete', 'create'], True),
            ('aws_s3_bucket.state', ['forget'], True),
            ('openstack_compute_instance_v2.controller', ['update'], True),
            ('openstack_compute_instance_v2.controller', ['create'], True),
        ]:
            with self.subTest(actions=actions), self.assertRaisesRegex(ValueError, 'Refusing'):
                deployment.check_plan({'resource_changes': [{'mode': 'managed', 'address': address,
                    'change': {'actions': actions}}]}, had_controller)
        deployment.check_plan({'resource_changes': [{'mode': 'managed',
            'address': 'openstack_compute_instance_v2.controller', 'change': {'actions': ['create']}}]}, False)

    def test_destructive_plan_is_rejected_before_apply_and_build_gets_no_cluster_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            calls, configs = [], []
            def command(arguments, _directory, **_kwargs):
                calls.append(arguments)
                if 'pull' in arguments:
                    return json.dumps({'resources': []})
                if 'show' in arguments:
                    return json.dumps({'resource_changes': [{'mode': 'managed', 'address': 'fixture',
                        'change': {'actions': ['delete']}}]})
                return ''
            def build(config, _format, path, _source):
                configs.append(config)
                cli.output_directory(path)
                cli.private_write(path / 'guaranine-kairos.qcow2', 'DISPOSABLE-IMAGE')
            environment = {'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': 'a' * 40,
                'DEPLOY_COMMIT': 'a' * 40, 'OVH_STATE_BUCKET': 'fixture', 'OVH_STORAGE_REGION': 'bhs',
                'OS_REGION_NAME': 'BHS5', 'TAILSCALE_AUTH_KEY': 'tskey-auth-DISPOSABLE',
                'SSH_PUBLIC_KEY': 'ssh-ed25519 PUBLIC', 'KAIROS_IMAGE': 'fixture@sha256:' + 'b' * 64,
                'ENROLLMENT_EXPIRES': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
            with (patch.dict(os.environ, environment), patch.object(cli, 'private_command', side_effect=command),
                  patch.object(cli, 'build_image', side_effect=build),
                  patch.object(deployment, 'ensure_terraform', return_value=directory / 'terraform'),
                  contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'Refusing destructive')):
                deployment.stage_controller(directory)
            self.assertFalse(any('apply' in call for call in calls))
            text = cli.cloud_yaml(configs[0])
            self.assertNotIn('path: /etc/rancher/k3s/token', text)
            self.assertNotIn('path: /etc/rancher/k3s/agent-token', text)

    def test_dispatch_refuses_unrestricted_environment_before_minting_or_uploading_a_key(self):
        responses = [json.dumps({'nameWithOwner': 'fixture/repo', 'defaultBranchRef': {'name': 'main'}}),
                     'a' * 40, '[]', json.dumps({'branch_policies': []})]
        with (patch.object(cli, 'operator_directory', return_value=Path('/tmp')),
              patch.object(cli, 'operator_settings', return_value={}),
              patch.object(cli, 'private_command', side_effect=responses) as command,
              patch.object(cli, 'mint') as mint, self.assertRaisesRegex(ValueError, 'only the main branch')):
            cli.deploy_controller(None)
        mint.assert_not_called()
        self.assertFalse(any('secret' in call.args[0] for call in command.call_args_list))
