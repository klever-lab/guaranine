#!/usr/bin/env python3
"""Exercise the actual nftables rules in disposable Linux network namespaces."""
import contextlib
import json
import os
from pathlib import Path
import pwd
import socket
import subprocess
import sys
import threading

import yaml


SCRIPT = Path(__file__).resolve()


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout


def serve():
    def worker(listener, kind):
        with listener:
            if kind == socket.SOCK_STREAM:
                while True:
                    connection, _ = listener.accept()
                    with connection:
                        connection.sendall(connection.recv(32))
            else:
                while True:
                    data, address = listener.recvfrom(32)
                    listener.sendto(data, address)
    # Bind synchronously so a failed fixture cannot silently look like isolation.
    for port, kind in ((18080, socket.SOCK_STREAM), (53, socket.SOCK_STREAM),
                       (53, socket.SOCK_DGRAM), (22, socket.SOCK_STREAM)):
        listener = socket.socket(socket.AF_INET6, kind)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.bind(('::', port))
        if kind == socket.SOCK_STREAM:
            listener.listen()
        thread = threading.Thread(target=worker, args=(listener, kind), daemon=True)
        thread.start()


def probe(address, port, protocol, uid, mark):
    kind = socket.SOCK_DGRAM if protocol == 'udp' else socket.SOCK_STREAM
    os.setuid(int(uid))
    with socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET, kind) as stream:
        if int(mark):
            stream.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, int(mark))
        stream.settimeout(0.5)
        try:
            if kind == socket.SOCK_DGRAM:
                stream.sendto(b'firewall-fixture', (address, int(port)))
                success = stream.recvfrom(32)[0] == b'firewall-fixture'
            else:
                stream.connect((address, int(port)))
                stream.sendall(b'firewall-fixture')
                success = stream.recv(32) == b'firewall-fixture'
        except OSError:
            success = False
    print(json.dumps(success))


def exercise():
    run('ip', 'link', 'set', 'lo', 'up')
    run('sysctl', '-qw', 'net.ipv4.ip_forward=1', 'net.ipv6.conf.all.forwarding=1')
    with contextlib.ExitStack() as cleanup:
        def peer(interface, addresses, remote_addresses=()):
            process = subprocess.Popen(['unshare', '--net', sys.executable, str(SCRIPT), '--server'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            def stop():
                process.terminate()
                process.wait(timeout=5)
            cleanup.callback(stop)
            if process.stdout.readline().strip() != 'READY':
                raise RuntimeError('Fixture server failed to start')
            prefix = ['nsenter', '-t', str(process.pid), '--net']
            run('ip', 'link', 'add', interface, 'type', 'veth', 'peer', 'name', interface + '-peer')
            run('ip', 'link', 'set', interface + '-peer', 'netns', str(process.pid))
            run('ip', 'link', 'set', interface, 'up')
            run(*prefix, 'ip', 'link', 'set', 'lo', 'up')
            run(*prefix, 'ip', 'link', 'set', interface + '-peer', 'up')
            for local, other in addresses:
                run('ip', 'address', 'add', local, 'dev', interface, 'nodad')
                run(*prefix, 'ip', 'address', 'add', other, 'dev', interface + '-peer', 'nodad')
            for address in remote_addresses:
                run(*prefix, 'ip', 'address', 'add', address, 'dev', 'lo', 'nodad')
            return prefix

        lan = peer('lan0', [('203.0.113.2/24', '203.0.113.1/24'),
                            ('192.168.50.2/24', '192.168.50.1/24'),
                            ('2001:db8:1::2/64', '2001:db8:1::1/64'), ('fd00:1::2/64', 'fd00:1::1/64')],
                   ['198.51.100.1/32', '2001:db8:2::1/128'])
        pod = peer('cni0', [('10.42.0.1/24', '10.42.0.2/24'), ('fd42::1/64', 'fd42::2/64')])
        tailnet = peer('tailscale0', [('100.64.0.1/24', '100.64.0.2/24')])
        for prefix, family, destination, gateway in [
                ([], '-4', '198.51.100.0/24', '203.0.113.1'),
                ([], '-6', '2001:db8:2::/64', '2001:db8:1::1'),
                (pod, '-4', 'default', '10.42.0.1'), (pod, '-6', 'default', 'fd42::1'),
                (lan, '-4', '10.42.0.0/24', '203.0.113.2'), (lan, '-6', 'fd42::/64', '2001:db8:1::2')]:
            run(*prefix, 'ip', family, 'route', 'add', destination, 'via', gateway)
        serve()

        def check(prefix, address, allowed, port=18080, protocol='tcp', uid=0, mark=0):
            output = run(*prefix, sys.executable, str(SCRIPT), '--probe', address, str(port), protocol,
                         str(uid), str(mark))
            if json.loads(output) != allowed:
                raise AssertionError(f'{address}:{port}/{protocol} uid={uid} expected allowed={allowed}')

        local = ['203.0.113.1', '192.168.50.1', '2001:db8:1::1', 'fd00:1::1']
        for prefix in ([], pod):
            for address in local:
                check(prefix, address, True)
                check(prefix, address, True, port=53)
                check(prefix, address, True, port=53, protocol='udp')
            for address in ('198.51.100.1', '2001:db8:2::1'):
                check(prefix, address, True)
        check(lan, '203.0.113.2', True, port=22)
        check(lan, '10.42.0.2', True)
        check(lan, '2001:db8:1::2', True, port=22)
        check(lan, 'fd42::2', True)
        pings = (([], '203.0.113.1'), (lan, '203.0.113.2'),
                 ([], '2001:db8:1::1'), (lan, '2001:db8:1::2'))
        for prefix, address in pings:
            run(*prefix, 'ping', '-n', '-c', '1', '-W', '1', address)

        base = yaml.safe_load((SCRIPT.parents[1] / 'kairos/config.yaml').read_text())
        rules = next(file['content'] for step in base['stages']['initramfs'] for file in step.get('files', [])
                     if file['path'] == '/etc/nftables.conf')
        run('nft', '-f', '-', input=rules)
        run('nft', 'add', 'element', 'inet', 'guaranine', 'lan4', '{ 203.0.113.0/24 }')
        run('nft', 'add', 'element', 'inet', 'guaranine', 'lan6', '{ 2001:db8:1::/64 }')
        index = str(socket.if_nametoindex('lan0'))
        run('nft', 'add', 'element', 'inet', 'guaranine', 'connected_interfaces', '{ ' + index + ' }')
        for prefix in ([], pod):
            for address in local:
                check(prefix, address, False)
                check(prefix, address, False, port=53)
                check(prefix, address, False, port=53, protocol='udp')
            for address in ('198.51.100.1', '2001:db8:2::1'):
                check(prefix, address, True)
        check(lan, '203.0.113.2', False, port=22)
        check(lan, '10.42.0.2', False)
        check(lan, '2001:db8:1::2', False, port=22)
        check(lan, 'fd42::2', False)
        for prefix, address in pings:
            if subprocess.run([*prefix, 'ping', '-n', '-c', '1', '-W', '1', address],
                              capture_output=True).returncode == 0:
                raise AssertionError('LAN echo traffic was allowed')
        check(tailnet, '100.64.0.1', True, port=22)
        check([], '100.64.0.2', True, port=22)
        resolver = pwd.getpwnam('systemd-resolve').pw_uid
        for protocol in ('tcp', 'udp'):
            check([], '203.0.113.1', True, port=53, protocol=protocol, uid=resolver)
            check([], '2001:db8:1::1', True, port=53, protocol=protocol, uid=resolver)
            check([], '203.0.113.1', False, port=53, protocol=protocol, uid=1000)
        check([], '203.0.113.1', True, port=53, protocol='udp', mark=0x80000)
        for target, name, prefix in (('198.51.100.1', 'lan4', '198.51.100.0/24'),
                                     ('2001:db8:2::1', 'lan6', '2001:db8:2::/64')):
            run('nft', 'add', 'element', 'inet', 'guaranine', name, '{ ' + prefix + ' }')
            check([], target, False)
            check(pod, target, False)
            run('nft', 'delete', 'element', 'inet', 'guaranine', name, '{ ' + prefix + ' }')
            check([], target, True)
        run('nft', 'flush', 'set', 'inet', 'guaranine', 'connected_interfaces')
        check([], '198.51.100.1', False)
        check(pod, '198.51.100.1', False)
        check([], '2001:db8:2::1', False)
        check(pod, '2001:db8:2::1', False)
        print('Firewall packet checks passed: LAN blocked, internet/Tailscale preserved, DNS restricted, unconfigured egress blocked')


if __name__ == '__main__':
    if sys.argv[1:] == ['--server']:
        serve()
        print('READY', flush=True)
        sys.stdin.read()
    elif sys.argv[1:2] == ['--probe']:
        probe(*sys.argv[2:])
    elif sys.argv[1:] == ['--isolated']:
        exercise()
    else:
        namespace = [] if os.geteuid() == 0 else ['--user', '--map-auto', '--map-root-user']
        subprocess.run(['unshare', *namespace, '--net', sys.executable, str(SCRIPT), '--isolated'], check=True)
