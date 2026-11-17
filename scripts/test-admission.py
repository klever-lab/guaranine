#!/usr/bin/env python3
"""Exercise real admission and RBAC in a disposable, loopback-only API server.

No kubelet, scheduler, production kubeconfig or container execution is involved.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOLS_VERSION = '1.37.0'
TOOLS_SHA256 = 'f03faedfc68d2a78cee5cb02618bb44e7f67044f4c5fc957f72d3307fda61a9c'
PROVISIONER = 'system:serviceaccount:openebs:openebs-localpv'
LABELER = 'system:serviceaccount:kairos-operator:kairos-node-labeler'
OPERATOR = 'system:serviceaccount:kairos-operator:operator-kairos-operator'
VOLUME = 'pvc-12345678-1234-1234-1234-123456789abc'


def binaries(directory):
    if override := os.environ.get('GUARANINE_TEST_BIN_DIR'):
        return Path(override).resolve()
    archive = directory / 'envtest.tar.gz'
    url = f'https://github.com/kubernetes-sigs/controller-tools/releases/download/envtest-v{TOOLS_VERSION}/envtest-v{TOOLS_VERSION}-linux-amd64.tar.gz'
    urllib.request.urlretrieve(url, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != TOOLS_SHA256:
        raise ValueError('Admission-test tools checksum mismatch')
    with tarfile.open(archive) as package:
        for name in ('etcd', 'kube-apiserver', 'kubectl'):
            member, = [m for m in package if m.isfile() and Path(m.name).name == name]
            target = directory / name
            target.write_bytes(package.extractfile(member).read())
            target.chmod(0o700)
    return directory


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def start(stack, command, log):
    output = stack.enter_context(log.open('w'))
    process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)

    def stop():
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    stack.callback(stop)
    return process


def resource_path(obj):
    group = obj['apiVersion']
    prefix = '/api/v1' if group == 'v1' else '/apis/' + group
    namespace = obj.get('metadata', {}).get('namespace')
    if namespace:
        prefix += '/namespaces/' + namespace
    plurals = {'StorageClass': 'storageclasses', 'Policy': 'policies'}
    kind = obj['kind'].lower()
    return prefix + '/' + plurals.get(obj['kind'], kind[:-1] + 'ies' if kind.endswith('policy') else kind + 's')


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='guaranine-admission-') as temporary, ExitStack() as stack:
        directory = Path(temporary)
        tools = binaries(directory)
        cert, key = directory / 'cert.pem', directory / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=guaranine-disposable', '-addext', 'subjectAltName=IP:127.0.0.1',
                        '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
        token = secrets.token_urlsafe(32)
        (directory / 'tokens.csv').write_text(token + ',fixture-admin,fixture-admin,"system:masters"\n')
        client, peer, api = port(), port(), port()
        etcd_url, peer_url = f'http://127.0.0.1:{client}', f'http://127.0.0.1:{peer}'
        start(stack, [str(tools / 'etcd'), '--name=fixture', '--data-dir=' + str(directory / 'etcd'),
                      '--listen-client-urls=' + etcd_url, '--advertise-client-urls=' + etcd_url,
                      '--listen-peer-urls=' + peer_url, '--initial-advertise-peer-urls=' + peer_url,
                      '--initial-cluster=fixture=' + peer_url], directory / 'etcd.log')
        server = start(stack, [str(tools / 'kube-apiserver'), '--bind-address=127.0.0.1',
                       '--advertise-address=127.0.0.1', f'--secure-port={api}',
                       '--endpoint-reconciler-type=none',
                       '--etcd-servers=' + etcd_url, '--service-cluster-ip-range=10.77.0.0/24',
                       '--authorization-mode=Node,RBAC', '--anonymous-auth=false',
                       '--allow-privileged=true',
                       '--token-auth-file=' + str(directory / 'tokens.csv'),
                       '--tls-cert-file=' + str(cert), '--tls-private-key-file=' + str(key),
                       '--client-ca-file=' + str(cert), '--service-account-issuer=https://fixture.invalid',
                       '--service-account-key-file=' + str(key), '--service-account-signing-key-file=' + str(key)],
                       directory / 'apiserver.log')
        context = ssl.create_default_context(cafile=str(cert))

        def request(method, path, body=None, *, user=None, bearer=None):
            headers = {'Authorization': 'Bearer ' + (bearer or token),
                       'Content-Type': 'application/merge-patch+json' if method == 'PATCH' else 'application/json'}
            if user:
                headers['Impersonate-User'] = user
                headers['Impersonate-Group'] = 'system:authenticated'
            req = urllib.request.Request(f'https://127.0.0.1:{api}' + path,
                data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, context=context, timeout=10) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        deadline = time.monotonic() + 30
        while True:
            try:
                if request('GET', '/api/v1/namespaces')[0] == 200:
                    break
            except (OSError, urllib.error.URLError):
                pass
            if server.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError((directory / 'apiserver.log').read_text()[-6000:])
            time.sleep(0.2)

        def create(obj, *, user=None, dry=False, denied_by=None):
            path = resource_path(obj) + ('?dryRun=All' if dry else '')
            code, result = request('POST', path, obj, user=user)
            if denied_by:
                if code not in (403, 422) or denied_by not in result.get('message', ''):
                    raise AssertionError(f'Expected {denied_by} denial, got {code}: {result}')
            elif code not in (200, 201, 409):
                raise AssertionError(f'{obj["kind"]}: {code}: {result}')
            return result

        for name in ('openebs', 'kairos-operator'):
            create({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': name}})
        for path in (ROOT / 'kubernetes/storage.yaml', ROOT / 'kubernetes/hardening.yaml'):
            for obj in yaml.safe_load_all(path.read_text()):
                if obj and obj['kind'] != 'Deployment':
                    create(obj)
        rendered = subprocess.run([str(tools / 'kubectl'), 'kustomize', str(ROOT / 'kubernetes/kairos-operator')],
                                  check=True, capture_output=True).stdout
        for obj in yaml.safe_load_all(rendered):
            if obj and obj['kind'] in ('ServiceAccount', 'ClusterRole', 'ClusterRoleBinding', 'Role', 'RoleBinding'):
                create(obj)

        helper = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': 'storage-fixture', 'namespace': 'openebs'},
                  'spec': {'serviceAccountName': 'openebs-localpv-helper', 'restartPolicy': 'Never',
                           'volumes': [{'name': 'data', 'hostPath': {'path': '/usr/local/openebs/local'}}],
                           'containers': [{'name': 'local-path-init',
                            'image': 'openebs/linux-utils:4.6.0@sha256:a3043d266b1c06fb42377ced0c3fc190c66e1e83ae1efda7670ed73feb4ede14',
                            'command': ['mkdir', '-m', '0770', '-p', '/data/' + VOLUME],
                            'securityContext': {'privileged': True},
                            'volumeMounts': [{'name': 'data', 'mountPath': '/data/'}]}]}}
        deadline = time.monotonic() + 30
        while True:
            code, result = request('POST', resource_path(helper) + '?dryRun=All', helper, user=PROVISIONER)
            if code == 201 and result['spec']['containers'][0]['securityContext'].get('privileged') is False:
                break
            if time.monotonic() > deadline:
                raise AssertionError(f'Native helper mutation did not converge: {code}: {result}')
            time.sleep(0.2)
        security = result['spec']['containers'][0]['securityContext']
        assert security['capabilities'] == {'drop': ['ALL']}
        assert security['readOnlyRootFilesystem'] and not security['allowPrivilegeEscalation']
        assert security['seccompProfile']['type'] == 'RuntimeDefault'
        assert len(result['spec']['volumes']) == 1
        assert result['spec']['volumes'][0]['hostPath']['type'] == 'DirectoryOrCreate'
        assert result['spec']['volumes'][0]['hostPath']['path'] == '/usr/local/openebs/local/' + VOLUME
        assert result['spec']['containers'][0]['command'] == ['chmod', '0770', '/data']

        for change in (
            lambda p: p['spec']['volumes'][0]['hostPath'].update(path='/'),
            lambda p: p['spec']['containers'][0].update(command=['sh', '-c', 'true']),
            lambda p: p['spec']['containers'][0]['command'].__setitem__(4, '/data/../outside'),
            lambda p: p['spec'].update(hostNetwork=True),
            lambda p: p['spec'].update(serviceAccountName='openebs-localpv'),
            lambda p: p['spec']['containers'][0].update(env=[{'name': 'LD_PRELOAD', 'value': '/data/code'}]),
        ):
            bad = deepcopy(helper)
            change(bad)
            create(bad, user=PROVISIONER, dry=True, denied_by='guaranine-storage-helpers')

        volume = {'apiVersion': 'v1', 'kind': 'PersistentVolume', 'metadata': {'name': VOLUME},
                  'spec': {'capacity': {'storage': '1Gi'}, 'accessModes': ['ReadWriteOnce'],
                           'storageClassName': 'openebs-hostpath', 'persistentVolumeReclaimPolicy': 'Retain',
                           'local': {'path': '/usr/local/openebs/local/' + VOLUME},
                           'nodeAffinity': {'required': {'nodeSelectorTerms': [{'matchExpressions': [
                               {'key': 'kubernetes.io/hostname', 'operator': 'In', 'values': ['worker-one']}]}]}}}}
        create(volume, user=PROVISIONER, dry=True)
        for source in ({'local': {'path': '/etc'}}, {'local': {'path': '/usr/local/openebs/local/other'}},
                       {'hostPath': {'path': '/'}}):
            bad = deepcopy(volume)
            bad['spec'].pop('local')
            bad['spec'].update(source)
            create(bad, user=PROVISIONER, dry=True, denied_by='guaranine-storage-volumes')

        for name in ('worker-one', 'worker-two'):
            create({'apiVersion': 'v1', 'kind': 'Node', 'metadata': {'name': name,
                    'labels': {'kubernetes.io/hostname': name, 'kairos.io/managed': 'true'}}, 'spec': {}})

        create({'apiVersion': 'v1', 'kind': 'ServiceAccount',
                'metadata': {'name': 'kairos-node-labeler', 'namespace': 'kairos-operator'}})
        labeler = create({'apiVersion': 'v1', 'kind': 'Pod',
            'metadata': {'name': 'labeler-fixture', 'namespace': 'kairos-operator'},
            'spec': {'nodeName': 'worker-one', 'serviceAccountName': 'kairos-node-labeler',
                     'containers': [{'name': 'node-labeler', 'image': 'example.invalid/fixture:never-executed',
                         'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000},
                         'volumeMounts': [{'name': 'host-etc', 'mountPath': '/host/etc', 'readOnly': True}]}],
                     'volumes': [{'name': 'host-etc', 'hostPath': {'path': '/etc'}}]}})
        reader = labeler['spec']['containers'][0]
        assert reader['securityContext']['capabilities'] == {'drop': ['ALL']}
        assert reader['securityContext']['readOnlyRootFilesystem']
        assert not reader['securityContext']['allowPrivilegeEscalation']
        host_paths = {v['hostPath']['path'] for v in labeler['spec']['volumes'] if 'hostPath' in v}
        assert host_paths == {'/etc/kairos-release', '/etc/os-release'}, host_paths
        assert '/host/etc' not in {m['mountPath'] for m in reader['volumeMounts']}
        token_path = '/api/v1/namespaces/kairos-operator/serviceaccounts/kairos-node-labeler/token'
        token_request = {'apiVersion': 'authentication.k8s.io/v1', 'kind': 'TokenRequest',
                         'spec': {'audiences': ['https://fixture.invalid'], 'expirationSeconds': 600,
                                  'boundObjectRef': {'apiVersion': 'v1', 'kind': 'Pod',
                                      'name': 'labeler-fixture', 'uid': labeler['metadata']['uid']}}}
        bound_token = request('POST', token_path, token_request)[1]['status']['token']
        token_request['spec'].pop('boundObjectRef')
        unbound_token = request('POST', token_path, token_request)[1]['status']['token']

        def node_patch(name, body, *, bound='worker-one', denied=False):
            code, result = request('PATCH', '/api/v1/nodes/' + name + '?dryRun=All', body,
                                   bearer=bound_token if bound else unbound_token)
            if denied:
                assert code in (403, 422) and 'guaranine-node-labeler' in result.get('message', ''), result
            else:
                assert code == 200, result

        node_patch('worker-one', {'metadata': {'labels': {'kubernetes.io/hostname': 'worker-one', 'kairos.io/id': 'kairos'}}})
        node_patch('worker-two', {'metadata': {'annotations': {'kairos.io/name': 'other'}}}, denied=True)
        node_patch('worker-one', {'spec': {'unschedulable': True}}, denied=True)
        node_patch('worker-one', {'spec': {'taints': [{'key': 'fixture', 'effect': 'NoExecute'}]}}, denied=True)
        node_patch('worker-one', {'metadata': {'labels': {'node-role.kubernetes.io/control-plane': 'true'}}}, denied=True)
        node_patch('worker-one', {'metadata': {'annotations': {'kairos.io/name': 'own'}}}, bound=None, denied=True)

        for namespace, expected in (('kairos-operator', True), ('openebs', False), ('default', False)):
            review = {'apiVersion': 'authorization.k8s.io/v1', 'kind': 'SelfSubjectAccessReview',
                      'spec': {'resourceAttributes': {'namespace': namespace, 'verb': 'create', 'resource': 'pods'}}}
            assert request('POST', resource_path(review), review, user=OPERATOR)[1]['status']['allowed'] == expected
        # Mutation is mandatory: removing its binding must not admit the original
        # upstream privileged helper, even while the namespace permits host mounts.
        request('DELETE', '/apis/admissionregistration.k8s.io/v1/mutatingadmissionpolicybindings/guaranine-host-readers')
        deadline = time.monotonic() + 10
        while True:
            code, result = request('POST', resource_path(helper) + '?dryRun=All', helper, user=PROVISIONER)
            if code in (403, 422) and 'guaranine-storage-helpers' in result.get('message', ''):
                break
            if time.monotonic() > deadline:
                raise AssertionError('Missing mutation did not fail closed')
            time.sleep(0.1)
        print('Disposable API checks passed: helper confinement, PV paths, node identity/metadata and operator RBAC.')


if __name__ == '__main__':
    main()
