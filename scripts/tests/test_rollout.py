"""Check rollout targeting, failure reporting and native configuration submission."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guaranine as cli


class RolloutTests(unittest.TestCase):
    def node(self, name, ready='True'):
        return {'metadata': {'name': name}, 'status': {'conditions': [{'type': 'Ready', 'status': ready}]}}

    def args(self):
        return SimpleNamespace(config=str(cli.ROOT / 'kairos/lid.yaml'), node=None)

    def run_rollout(self, responses, args=None):
        def command(arguments, **kwargs):
            return subprocess.CompletedProcess(arguments, 0, json.dumps(next(responses)), '')
        with (patch.object(cli.subprocess, 'run', side_effect=command) as run,
              patch.object(cli.time, 'sleep'), contextlib.redirect_stdout(io.StringIO())):
            cli.rollout_config(args or self.args())
        return run

    def test_submits_a_fresh_operation_with_a_fixed_worker_set_and_waits_for_all_nodes(self):
        responses = iter([
            {'items': [self.node('worker-two'), self.node('worker-one')]}, {'items': []},
            {'metadata': {'name': 'config-lid-test'}},
            {'status': {'phase': 'Completed', 'nodeStatuses': {'worker-one': {'phase': 'Completed'}}}},
            {'status': {'phase': 'Completed', 'nodeStatuses': {
                'worker-one': {'phase': 'Completed'}, 'worker-two': {'phase': 'Completed'}}}},
        ])
        run = self.run_rollout(responses)
        self.assertEqual(run.call_count, 5)
        selection = run.call_args_list[0].args[0]
        self.assertIn('kairos.io/managed=true,!node-role.kubernetes.io/control-plane,!node-role.kubernetes.io/master', selection)
        submission = run.call_args_list[2]
        self.assertIn('create', submission.args[0])
        operation = yaml.safe_load(submission.kwargs['input'])
        self.assertNotIn('name', operation['metadata'])
        self.assertEqual(operation['metadata']['generateName'], 'config-lid-')
        spec = operation['spec']
        self.assertEqual(spec['nodeSelector']['matchExpressions'], [
            {'key': 'kubernetes.io/hostname', 'operator': 'In', 'values': ['worker-one', 'worker-two']}])
        self.assertEqual(spec['concurrency'], 1)
        self.assertIs(spec['stopOnFailure'], True)
        self.assertEqual(spec['backoffLimit'], 0)
        self.assertIs(spec['cordon'], False)
        self.assertIs(spec['rebootOnSuccess'], False)
        self.assertIs(spec['drainOptions']['enabled'], False)
        script = spec['command'][-1]
        self.assertIn((cli.ROOT / 'kairos/lid.yaml').read_text().rstrip(), script)
        self.assertLess(script.index('--strict-validation validate'), script.index('mv -f'))
        self.assertIn('export SYSTEMCTL_FORCE_BUS=1', script)
        self.assertEqual(script.count('run-stage --strict --override-cloud-init-paths "${activation#"$rollout_host"}" guaranine-rollout-'), 2)
        self.assertNotIn('"${activation#"$rollout_host"}" initramfs', script)
        self.assertNotIn('"${activation#"$rollout_host"}" boot', script)
        activation = yaml.safe_load(script.split("<<'GUARANINE_ACTIVATION'\n", 1)[1].split('\nGUARANINE_ACTIVATION', 1)[0])
        self.assertEqual([stage.rsplit('-', 1)[-1] for stage in activation['stages']], ['initramfs', 'boot'])
        self.assertEqual(list(activation['stages'].values()),
                         list(yaml.safe_load((cli.ROOT / 'kairos/lid.yaml').read_text())['stages'].values()))
        subprocess.run(['sh', '-n'], input=script, text=True, check=True)

    def test_single_node_selection_is_explicit(self):
        args = self.args()
        args.node = 'demure'
        run = self.run_rollout(iter([
            {'items': [self.node('demure')]}, {'items': []}, {'metadata': {'name': 'config-lid-test'}},
            {'status': {'phase': 'Completed', 'nodeStatuses': {'demure': {'phase': 'Completed'}}}},
        ]), args)
        selector = run.call_args_list[0].args[0][-3]
        self.assertTrue(selector.endswith(',kubernetes.io/hostname=demure'))

    def test_empty_or_unready_selection_never_submits(self):
        for nodes in ([], [self.node('demure', 'False')], [self.node('demure', 'Unknown')]):
            with self.subTest(nodes=nodes), patch.object(cli.subprocess, 'run', return_value=
                    subprocess.CompletedProcess([], 0, json.dumps({'items': nodes}), '')) as run:
                with self.assertRaises(ValueError):
                    cli.rollout_config(self.args())
                run.assert_called_once()

    def test_installation_profiles_and_private_paths_are_rejected_before_cluster_access(self):
        for path in (cli.ROOT / 'kairos/config.yaml', cli.ROOT / 'kairos/worker.yaml', Path('/tmp/private.yaml')):
            args = self.args()
            args.config = str(path)
            with self.subTest(path=path), patch.object(cli.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    cli.rollout_config(args)
                run.assert_not_called()

    def test_existing_active_operation_blocks_submission(self):
        for status in ({}, {'phase': 'Pending'}, {'phase': 'Running'}):
            responses = iter([{'items': [self.node('demure')]}, {'items': [{'status': status}]}])
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'already active'):
                self.run_rollout(responses)

    def test_failure_is_reported_without_retrying_the_host_operation(self):
        responses = iter([{'items': [self.node('demure')]}, {'items': []},
                          {'metadata': {'name': 'config-lid-test'}},
                          {'status': {'phase': 'Failed', 'nodeStatuses': {'demure': {'phase': 'Failed'}}}}])
        with self.assertRaisesRegex(ValueError, 'config-lid-test failed'):
            self.run_rollout(responses)

    def test_timeout_does_not_claim_success_or_cancel_the_cluster_operation(self):
        responses = iter([{'items': [self.node('demure')]}, {'items': []},
                          {'metadata': {'name': 'config-lid-test'}}])
        with patch.object(cli.time, 'monotonic', side_effect=[0, 601]):
            with self.assertRaisesRegex(ValueError, 'continues in the cluster'):
                self.run_rollout(responses)

    def test_api_error_is_visible_and_never_reported_as_success(self):
        with patch.object(cli.subprocess, 'run', return_value=
                subprocess.CompletedProcess([], 1, '', 'API unavailable')):
            with self.assertRaisesRegex(ValueError, 'API unavailable'):
                cli.rollout_config(self.args())


if __name__ == '__main__':
    unittest.main()
