"""The terminal snapshot must remain useful during monitoring and network outages."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guaranine as cli


def resource(kind, name, *, namespace='test', spec=None, status=None):
    return {'kind': kind, 'metadata': {'name': name, 'namespace': namespace, 'generation': 1},
            'spec': spec or {}, 'status': status or {}}


def node(name='worker', ready='True'):
    return resource('Node', name, status={'conditions': [
        {'type': 'Ready', 'status': ready, 'message': 'Kubelet stopped posting node status.'}]})


class StatusTests(unittest.TestCase):
    def snapshot(self, resources=None, *, tailscale=None, api=None, health=None, environment=None):
        if resources is None:
            resources = [node(), resource('Pod', 'app', spec={'nodeName': 'worker'}, status={
                'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]}),
                resource('Deployment', 'app', status={'readyReplicas': 1, 'observedGeneration': 1})]
        responses = {
            'tailscale': {'BackendState': 'Running', 'Peer': {}} if tailscale is None else tailscale,
            'kubectl': {'items': resources} if api is None else api,
        }
        def command(arguments, **kwargs):
            response = responses[arguments[0]]
            self.assertLessEqual(kwargs['timeout'], 15)
            if isinstance(response, Exception):
                raise response
            if isinstance(response, subprocess.CompletedProcess):
                return response
            return subprocess.CompletedProcess(arguments, 0, json.dumps(response), '')
        response = io.BytesIO(json.dumps(health if isinstance(health, dict) else {'database': 'ok'}).encode())
        response.status = 200
        output = io.StringIO()
        with (patch.dict(os.environ, environment or {}, clear=True),
              patch.object(cli.subprocess, 'run', side_effect=command) as run,
              patch.object(cli.urllib.request, 'urlopen', **(
                  {'side_effect': health} if isinstance(health, Exception) else {'return_value': response})) as probe,
              contextlib.redirect_stdout(output)):
            code = cli.main(['status'])
        return code, output.getvalue(), run, probe

    def test_healthy_cluster_includes_completed_jobs_and_intentionally_stopped_workloads(self):
        resources = [node(), resource('Pod', 'finished-job', status={'phase': 'Succeeded'}),
                     resource('Deployment', 'paused', spec={'replicas': 0}, status={'observedGeneration': 1}),
                     resource('StatefulSet', 'database', status={'readyReplicas': 1, 'observedGeneration': 1}),
                     resource('DaemonSet', 'agent', status={
                         'desiredNumberScheduled': 1, 'numberReady': 1, 'observedGeneration': 1})]
        tailscale = {'BackendState': 'Running', 'Peer': {
            'reader': {'HostName': 'sleeping-reader', 'Tags': ['tag:guaranine-kobo'], 'Online': False},
            'gateway': {'HostName': 'stopped-gateway', 'Tags': ['tag:guaranine-app-gateway'], 'Online': False},
            'personal': {'HostName': 'personal-phone', 'Online': True}}}
        code, output, run, probe = self.snapshot(resources, tailscale=tailscale)
        self.assertEqual(code, 0)
        self.assertIn('Overall: OK', output)
        self.assertIn('0 ready, 1 completed, 0 need attention', output)
        self.assertIn('Inactive (desired 0)', output)
        self.assertIn('StatefulSet/database', output)
        self.assertIn('DaemonSet/agent', output)
        self.assertIn('sleeping-reader', output)
        self.assertNotIn('personal-phone', output)
        self.assertEqual([call.args[0][0] for call in run.call_args_list], ['tailscale', 'kubectl'])
        kubectl = run.call_args_list[1].args[0]
        self.assertEqual(kubectl[2], 'get')
        self.assertNotIn('secrets', kubectl[3])
        self.assertIn('--all-namespaces', kubectl)
        self.assertTrue(probe.call_args.args[0].startswith('https://'))
        self.assertTrue(probe.call_args.args[0].endswith('/api/health'))

    def test_offline_worker_explains_blocked_storage_and_pending_pods_with_grafana_down(self):
        resources = [node(ready='Unknown'),
            resource('Deployment', 'grafana', namespace='monitoring', status={'observedGeneration': 1}),
            resource('Pod', 'grafana-new', namespace='monitoring', status={'phase': 'Pending', 'conditions': [
                {'type': 'PodScheduled', 'status': 'False',
                 'message': "No nodes match PersistentVolume's node affinity."}]}),
            resource('PersistentVolumeClaim', 'grafana', namespace='monitoring',
                     spec={'volumeName': 'grafana'}, status={'phase': 'Bound'}),
            resource('PersistentVolume', 'grafana', spec={'nodeAffinity': {'required': {'nodeSelectorTerms': [
                {'matchExpressions': [{'key': 'kubernetes.io/hostname', 'operator': 'In', 'values': ['worker']}]}]}}})]
        code, output, _, _ = self.snapshot(resources, health=urllib.error.HTTPError(
            'https://example.invalid/api/health', 502, 'Bad Gateway', {}, None))
        self.assertEqual(code, 1)
        for message in ('NotReady', 'Kubelet stopped', 'monitoring/grafana', 'Pending',
                        "PersistentVolume's node affinity", 'Bound', 'Node unavailable', '502', 'NEEDS ATTENTION'):
            self.assertIn(message, output)

    def test_running_unready_crashing_and_terminating_pods_are_reported(self):
        crash = resource('Pod', 'crash', status={'phase': 'Running', 'initContainerStatuses': [
            {'state': {'terminated': {'reason': 'Completed', 'exitCode': 0}}}], 'containerStatuses': [
            {'state': {'waiting': {'reason': 'CrashLoopBackOff'}}, 'restartCount': 5}]})
        unready = resource('Pod', 'unready', status={'phase': 'Running'})
        terminating = resource('Pod', 'old', status={'phase': 'Running'})
        terminating['metadata']['deletionTimestamp'] = '2026-01-01T00:00:00Z'
        code, output, _, _ = self.snapshot([node(), crash, unready, terminating])
        self.assertEqual(code, 1)
        self.assertIn('0 ready, 0 completed, 3 need attention', output)
        for message in ('CrashLoopBackOff', 'NotReady', 'Terminating'):
            self.assertIn(message, output)
        self.assertNotIn('Completed, CrashLoopBackOff', output)

    def test_node_pressure_empty_cluster_and_unbound_claim_are_not_healthy(self):
        pressured = node()
        pressured['status']['conditions'].append({'type': 'DiskPressure', 'status': 'True'})
        cases = [([pressured], 'DiskPressure'),
                 ([node(), resource('PersistentVolumeClaim', 'new', status={'phase': 'Pending'})], 'Pending'),
                 ([node(), resource('PersistentVolumeClaim', 'missing', status={'phase': 'Bound'})], 'Volume missing'),
                 ([], 'no nodes registered')]
        for resources, message in cases:
            with self.subTest(message=message):
                code, output, _, _ = self.snapshot(resources)
                self.assertEqual(code, 1)
                self.assertIn(message, output)

    def test_stale_pod_readiness_on_an_offline_node_does_not_count_as_healthy(self):
        pod = resource('Pod', 'stale', spec={'nodeName': 'worker'}, status={
            'phase': 'Running', 'conditions': [{'type': 'Ready', 'status': 'True'}]})
        code, output, _, _ = self.snapshot([node(ready='Unknown'), pod])
        self.assertEqual(code, 1)
        self.assertIn('0 ready, 0 completed, 1 need attention', output)
        self.assertIn('Node unavailable', output)

    def test_api_failure_still_checks_tailnet_and_grafana_without_printing_raw_data(self):
        code, output, _, probe = self.snapshot(api=subprocess.CompletedProcess(
            [], 1, 'PRIVATE RESPONSE MUST NOT BE PRINTED', 'API connection refused'))
        self.assertEqual(code, 1)
        self.assertIn('Tailscale: Running', output)
        self.assertIn('Kubernetes: unavailable (API connection refused)', output)
        self.assertIn('cluster health is unknown', output)
        self.assertIn('Grafana: HTTP 200', output)
        self.assertNotIn('PRIVATE RESPONSE', output)
        probe.assert_called_once()

    def test_timeouts_and_missing_tools_preserve_the_other_checks(self):
        for tool in ('tailscale', 'kubectl'):
            for error in (FileNotFoundError('Tool missing'), subprocess.TimeoutExpired(tool, 5),
                          subprocess.CompletedProcess([], 0, 'invalid JSON', '')):
                with self.subTest(tool=tool, error=error):
                    code, output, _, probe = self.snapshot(**{'tailscale' if tool == 'tailscale' else 'api': error})
                    self.assertEqual(code, 1)
                    self.assertIn('Tailscale: unavailable' if tool == 'tailscale' else 'Kubernetes: unavailable', output)
                    self.assertIn('Grafana: HTTP 200', output)
                    probe.assert_called_once()

    def test_disconnected_tailnet_offline_pi_and_unhealthy_grafana_require_attention(self):
        cases = [{'tailscale': {'BackendState': 'NeedsLogin'}},
                 {'tailscale': {'BackendState': 'Running', 'Peer': {
                     'pi': {'HostName': 'display', 'Tags': ['tag:guaranine-kiosk'], 'Online': False}}}},
                 {'health': {'database': 'failing'}}, {'health': urllib.error.URLError('DNS unavailable')}]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                code, output, _, _ = self.snapshot(**arguments)
                self.assertEqual(code, 1)
                self.assertIn('Overall: NEEDS ATTENTION', output)

    def test_kubeconfig_honors_ambient_context_and_explicit_operator_override(self):
        for override in (False, True):
            environment = {'KUBECONFIG': '/tmp/selected-context'}
            if override:
                environment['GUARANINE_OPERATOR_DIR'] = '/tmp/guaranine-status-fixture'
            with self.subTest(override=override):
                code, _, run, _ = self.snapshot(environment=environment)
                self.assertEqual(code, 0)
                self.assertEqual(run.call_args_list[1].kwargs['env']['KUBECONFIG'],
                    '/tmp/guaranine-status-fixture/kubeconfig' if override else '/tmp/selected-context')


if __name__ == '__main__':
    unittest.main()
