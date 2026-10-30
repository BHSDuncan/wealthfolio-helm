#!/usr/bin/env python3
"""Private existing-controller TLS/browser acceptance on a distinct synthetic release.

Uses only namespace resources and the existing controller. No trust-store or DNS
changes: private CA verification plus a process-local exact browser SPKI pin.
All credentials, private keys and raw payloads stay in an external mode0700 dir.
"""
import argparse
import base64
import datetime
import hashlib
import http.cookiejar
import json
import ipaddress
import os
import re
import traceback
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import argon2
import yaml
from client import Client

ROOT = Path(__file__).resolve().parents[2]
LABEL = 'wealthfolio.github.io/test-run'
HOST = 'wealthfolio-chart-test.example.test'
IMAGE = 'sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994'


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('kubeconfig', 'context', 'namespace', 'run-id', 'confirm-cluster-uid', 'ssh-host', 'ssh-identity'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--state-dir', type=Path, required=True)
    p.add_argument('--local-port', type=int, default=8443)
    p.add_argument('--release', default='wealthfolio-ingress-test')
    p.add_argument('--ingress-class', help='Existing class; omitted uses the cluster default')
    p.add_argument('--storage-class', help='Existing storage class; omitted uses the cluster default')
    p.add_argument('--ingress-address', default='127.0.0.1', help='Controller address reachable from the SSH host')
    p.add_argument('--ingress-port', type=int, default=443, help='Controller HTTPS port reachable from the SSH host')
    p.add_argument('--resume-private', action='store_true', help='Resume only matching owned inventory and privately generated credentials')
    a = p.parse_args()
    if not 1 <= a.ingress_port <= 65535:
        p.error('Ingress port must be between 1 and 65535')
    for value in (a.ingress_class, a.storage_class):
        if value is not None and not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', value):
            p.error('Ingress and storage classes must be valid Kubernetes names')
    try:
        ipaddress.ip_address(a.ingress_address)
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', a.ingress_address):
            p.error('Ingress address must be a hostname or IP address')
    return a


def route_values(a, host, origin, auth_name, tls_name):
    values = {'fullnameOverride': a.release, 'commonLabels': {LABEL: a.run_id},
              'auth': {'existingSecret': auth_name, 'cookieSecure': 'true'},
              'image': {'repository': 'ghcr.io/wealthfolio/wealthfolio', 'tag': '3.11.0', 'digest': IMAGE, 'pullPolicy': 'IfNotPresent'},
              'app': {'corsAllowOrigins': [origin]},
              'ingress': {'enabled': True, 'hosts': [host], 'tls': [{'secretName': tls_name, 'hosts': [host]}]}}
    if a.ingress_class:
        values['ingress']['className'] = a.ingress_class
    if a.storage_class:
        values['persistence'] = {'storageClass': a.storage_class}
    return values


def forwarding(a):
    address = '[' + a.ingress_address + ']' if ':' in a.ingress_address else a.ingress_address
    return f'127.0.0.1:{a.local_port}:{address}:{a.ingress_port}'


def main():
    a = arguments()
    os.umask(0o077)
    state = a.state_dir.resolve()
    if ROOT == state or ROOT in state.parents or (state.exists() and not a.resume_private):
        raise SystemExit('Use a new private state directory outside the repository, or explicit matching resume')
    if a.resume_private:
        assert state.is_dir() and not state.stat().st_mode & 0o077
    else:
        state.mkdir(mode=0o700)
    if not (1 <= a.local_port <= 65535) or not re.fullmatch(r'wealthfolio-ingress-test(?:-[0-9]+)?', a.release):
        raise SystemExit('Unexpected test release or local port')
    HOST = a.release + '.example.test'
    origin = f'https://{HOST}:{a.local_port}'
    base = ['kubectl', '--kubeconfig', a.kubeconfig, '--context', a.context, '-n', a.namespace]
    report = {'testId': 'T-INGRESS-TLS', 'status': 'FAIL', 'startedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'imageDigest': IMAGE, 'results': []}
    if a.resume_private:
        report = json.loads((state / 'report.json').read_text())
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
    report.update(chartVersion='0.1.0', source={'revision': revision.stdout.strip() if revision.returncode == 0 else 'UNBORN', 'dirty': True})
    stage = 'target-identity'
    tunnel = None
    old_resolver = socket.getaddrinfo
    def command(args, data=None):
        r = subprocess.run([str(x) for x in args], input=data, capture_output=True, timeout=700)
        if r.returncode:
            raise RuntimeError('Operation failed; raw output withheld')
        return r.stdout
    def get(resource, name=None):
        return json.loads(command(base + ['get', resource] + ([name] if name else []) + ['-o', 'json']))
    def create(manifest):
        item = json.loads(command(base + ['create', '-f', '-', '-o', 'json'], json.dumps(manifest).encode()))
        report.setdefault('objects', []).append({'kind': item['kind'], 'name': item['metadata']['name'], 'uid': item['metadata']['uid']})
    try:
        assert get('namespace', 'kube-system')['metadata']['uid'] == a.confirm_cluster_uid
        assert get('namespace', a.namespace)['metadata'].get('labels', {}).get(LABEL) == a.run_id
        assert command(base + ['auth', 'can-i', 'create', 'ingresses']).strip() == b'yes'
        if a.ingress_class:
            assert any(c['metadata']['name'] == a.ingress_class for c in get('ingressclasses')['items'])
        if a.resume_private:
            expected_uid = next(item['uid'] for item in report['objects'] if item['kind'] == 'Deployment')
            deployment = get('deployment', a.release)
            assert deployment['metadata']['uid'] == expected_uid and deployment['metadata'].get('labels', {}).get(LABEL) == a.run_id
        else:
            assert all(item['metadata']['name'] != a.release for item in get('deployments')['items'])
        report['kubernetesVersion'] = json.loads(command(base + ['version', '-o', 'json']))['serverVersion']['gitVersion']
        if not a.resume_private:
            stage = 'private-certificate'
            command(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2', '-subj', '/CN=Wealthfolio synthetic test CA', '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-keyout', state / 'ca.key', '-out', state / 'ca.crt'])
            command(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=' + HOST, '-keyout', state / 'tls.key', '-out', state / 'tls.csr'])
            (state / 'extensions').write_text(f'subjectAltName=DNS:{HOST}\nextendedKeyUsage=serverAuth\n')
            command(['openssl', 'x509', '-req', '-in', state / 'tls.csr', '-CA', state / 'ca.crt', '-CAkey', state / 'ca.key', '-CAcreateserial', '-days', '2', '-extfile', state / 'extensions', '-out', state / 'tls.crt'])
            command(['openssl', 'verify', '-CAfile', state / 'ca.crt', '-verify_hostname', HOST, state / 'tls.crt'])
            pubkey = command(['openssl', 'x509', '-in', state / 'tls.crt', '-pubkey', '-noout'])
            der = command(['openssl', 'pkey', '-pubin', '-outform', 'DER'], pubkey)
            spki = base64.b64encode(hashlib.sha256(der).digest()).decode()
            password = secrets.token_urlsafe(24)
            phc = argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, type=argon2.Type.ID).hash(password)
            master = base64.b64encode(secrets.token_bytes(32)).decode()
            (state / 'recovery.json').write_text(json.dumps({'password': password, 'masterKey': master}))
            auth_name, tls_name = a.release + '-auth', a.release + '-tls'
            stage = 'owned-secret-creation'
            create({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': auth_name, 'namespace': a.namespace, 'labels': {LABEL: a.run_id}}, 'type': 'Opaque', 'stringData': {'WF_SECRET_KEY': master, 'WF_AUTH_PASSWORD_HASH': phc}})
            create({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': tls_name, 'namespace': a.namespace, 'labels': {LABEL: a.run_id}}, 'type': 'kubernetes.io/tls', 'data': {'tls.crt': base64.b64encode((state / 'tls.crt').read_bytes()).decode(), 'tls.key': base64.b64encode((state / 'tls.key').read_bytes()).decode()}})
            values = route_values(a, HOST, origin, auth_name, tls_name)
            (state / 'values.yaml').write_text(yaml.safe_dump(values))
            stage = 'isolated-chart-install'
            command(['helm', 'install', a.release, ROOT / 'dist/wealthfolio-0.1.0.tgz', '--kubeconfig', a.kubeconfig, '--kube-context', a.context, '--namespace', a.namespace, '-f', state / 'values.yaml', '--wait', '--timeout', '10m'])
            for kind in ('deployment', 'service', 'pvc', 'ingress'):
                obj = get(kind, a.release)
                assert obj['metadata'].get('labels', {}).get(LABEL) == a.run_id
                report.setdefault('objects', []).append({'kind': obj['kind'], 'name': a.release, 'uid': obj['metadata']['uid']})
            ingress = get('ingress', a.release)
            route = ingress['spec']['rules'][0]['http']['paths'][0]
            assert route['path'] == '/' and route['pathType'] == 'Prefix'
            if a.ingress_class:
                assert ingress['spec']['ingressClassName'] == a.ingress_class
        else:
            stage = 'private-resume-verification'
            # The same locally generated key/subject signs this client-trust root;
            # the existing leaf's public-key signature remains valid. No server Secret changes.
            command(['openssl', 'req', '-x509', '-new', '-key', state / 'ca.key', '-days', '2', '-subj', '/CN=Wealthfolio synthetic test CA', '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-out', state / 'ca.crt'])
            command(['openssl', 'verify', '-x509_strict', '-CAfile', state / 'ca.crt', '-verify_hostname', HOST, state / 'tls.crt'])
            recovery = json.loads((state / 'recovery.json').read_text())
            password = recovery['password']
            pubkey = command(['openssl', 'x509', '-in', state / 'tls.crt', '-pubkey', '-noout'])
            der = command(['openssl', 'pkey', '-pubin', '-outform', 'DER'], pubkey)
            spki = base64.b64encode(hashlib.sha256(der).digest()).decode()
        stage = 'private-lan-tunnel'
        tunnel = subprocess.Popen(['ssh', '-i', a.ssh_identity, '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ExitOnForwardFailure=yes', '-N', '-L', forwarding(a), a.ssh_host], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if tunnel.poll() is not None:
                raise RuntimeError('Private SSH TLS tunnel failed')
            try:
                with socket.create_connection(('127.0.0.1', a.local_port), timeout=.2): break
            except OSError: time.sleep(.1)
        else: raise RuntimeError('Private TLS listener never became reachable')
        # Curl verifies the private CA, SNI and hostname with no insecure flags.
        command(['curl', '--fail', '--silent', '--show-error', '--noproxy', '*', '--cacert', state / 'ca.crt', '--resolve', f'{HOST}:{a.local_port}:127.0.0.1', origin + '/api/v1/healthz'])
        stage = 'https-origin-auth-fixture'
        def resolver(host, port, *args, **kwargs):
            return old_resolver('127.0.0.1' if host == HOST else host, port, *args, **kwargs)
        socket.getaddrinfo = resolver
        client = Client(origin, password)
        client.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(state / 'ca.crt'))), urllib.request.HTTPCookieProcessor(client.jar))
        stage = 'https-login'
        client.login()
        stage = 'secure-cookie-proof'
        assert any(cookie.name == 'wf_session' and cookie.secure and cookie.has_nonstandard_attr('HttpOnly') for cookie in client.jar)
        stage = 'https-allowed-origin-proof'
        _, allowed = client.request('GET', '/api/v1/healthz', headers={'Origin': origin})
        assert allowed.get('Access-Control-Allow-Origin') == origin and allowed.get('Access-Control-Allow-Credentials') == 'true'
        stage = 'https-denied-origin-proof'
        _, denied = client.request('GET', '/api/v1/healthz', headers={'Origin': 'https://disallowed.example.test'})
        assert denied.get('Access-Control-Allow-Origin') != 'https://disallowed.example.test'
        stage = 'https-two-profile-seed'
        if (state / 'fixture.json').exists():
            fixture = json.loads((state / 'fixture.json').read_text())
        else:
            fixture = client.seed()
        client.verify(fixture, encrypted=False, login=False)
        (state / 'fixture.json').write_text(json.dumps(fixture))
        stage = 'https-browser-login-data-logout'
        item = fixture[0]
        browser = {'password': password, 'profileId': item['profile'], 'accountName': item['name'], 'accountId': item['account'], 'vaultSha256': hashlib.sha256(item['marker'].encode()).hexdigest(), 'tlsSpkiSha256': spki, 'tlsHostname': HOST}
        (state / 'browser.json').write_text(json.dumps(browser))
        result = json.loads(command(['node', ROOT / 'tests/integration/browser.mjs', '--base-url', origin, '--state-file', state / 'browser.json']))
        assert result['status'] == 'PASS'
        report['results'] = [{'testId': name, 'status': 'PASS'} for name in ['existing-Ingress-root-Prefix-route', 'private-LAN-SSH-loopback-TLS', 'CA-chain-and-hostname-verified', 'exact-origin-CORS-denial', 'Secure-HttpOnly-cookie', 'two-profile-account-vault-fixture', 'HTTPS-browser-visible-account-vault-and-logout']]
        report['status'] = 'PASS'
        print('T-INGRESS-TLS: PASS existing Ingress/private CA TLS/LAN tunnel/origin/Secure cookie/browser account+vault/logout')
    except Exception as error:
        (state / 'private-error.txt').write_text(traceback.format_exc())
        report['failureStage'] = stage
        report['errorType'] = type(error).__name__
        print('T-INGRESS-TLS: FAIL at ' + stage + '; private payloads withheld', file=sys.stderr)
        return 1
    finally:
        socket.getaddrinfo = old_resolver
        if tunnel:
            tunnel.terminate()
            tunnel.wait(timeout=15)
        report['finishedAt'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report['retention'] = 'Only isolated owned namespace resources retained; no shared lifecycle release modified'
        (state / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        # Auth/fixture/key files remain private only until recovery/evidence review. No automated object deletion.
    return 0


if __name__ == '__main__':
    sys.exit(main())
