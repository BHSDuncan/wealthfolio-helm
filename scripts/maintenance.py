#!/usr/bin/env python3
"""Stopped whole-data backup; empty-claim restore. Credentials never enter arguments."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import uuid

UTILITY = 'docker.io/library/busybox:1.37.0@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec'


def validate_archive(path):
    """Reject traversal, links, devices and other archive extraction surprises."""
    with tarfile.open(path, 'r:*') as archive:
        members = archive.getmembers()
        if not members:
            raise ValueError('Archive is empty')
        for member in members:
            p = Path(member.name)
            if p.is_absolute() or '..' in p.parts or not (member.isdir() or member.isfile()):
                raise ValueError('Unsafe archive member')


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['backup', 'restore'])
    for name in ('kubeconfig', 'context', 'namespace', 'release', 'claim', 'confirm-cluster-uid'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--output', type=Path)
    p.add_argument('--input', type=Path)
    p.add_argument('--sha256')
    p.add_argument('--confirm-stopped', action='store_true', required=True)
    p.add_argument('--confirm-empty-test-claim', action='store_true')
    return p


def main():
    a = parser().parse_args()
    for name in ('namespace', 'release', 'claim'):
        if not re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', getattr(a, name)):
            raise SystemExit('Invalid Kubernetes name: ' + name)
    if not Path(a.kubeconfig).is_file():
        raise SystemExit('Explicit kubeconfig does not exist')
    base = [os.environ.get('KUBECTL_BIN', 'kubectl'), '--kubeconfig', a.kubeconfig, '--context', a.context]
    def run(args, **kw):
        r = subprocess.run(base + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        if r.returncode:
            raise RuntimeError('kubectl operation failed; workload remains stopped; inspect owned maintenance pod/events')
        return r.stdout
    def get(args):
        return json.loads(run(args + ['-o', 'json']))
    if get(['get', 'namespace', 'kube-system'])['metadata']['uid'] != a.confirm_cluster_uid:
        raise SystemExit('Cluster identity mismatch')
    scoped = ['-n', a.namespace]
    ns = get(['get', 'namespace', a.namespace])
    claims = get(scoped + ['get', 'pvc', a.claim])
    deps = get(scoped + ['get', 'deployments', '-l', 'app.kubernetes.io/instance=' + a.release])['items']
    if len(deps) != 1 or deps[0]['spec']['replicas'] != 0:
        raise SystemExit('Expected exactly one stopped release Deployment; scale to zero and wait first')
    source_claims = [v.get('persistentVolumeClaim', {}).get('claimName') for v in deps[0]['spec']['template']['spec']['volumes']]
    if a.operation == 'backup' and a.claim not in source_claims:
        raise SystemExit('Claim does not belong to the selected release')
    active = get(scoped + ['get', 'pods'])['items']
    if any(v.get('persistentVolumeClaim', {}).get('claimName') == a.claim for pod in active for v in pod['spec'].get('volumes', [])):
        raise SystemExit('A pod still references this claim; no concurrent maintenance writers allowed')
    if a.operation == 'restore':
        if not a.input or not a.sha256 or not a.confirm_empty_test_claim:
            raise SystemExit('Restore requires --input --sha256 --confirm-empty-test-claim')
        # Valuable-data overwrite is deliberately unsupported by this safe helper.
        owner = ns['metadata'].get('labels', {}).get('wealthfolio.github.io/test-run')
        if not owner or claims['metadata'].get('labels', {}).get('wealthfolio.github.io/test-run') != owner:
            raise SystemExit('Restore supports only test-run-labeled synthetic claims in its matching namespace')
        if hashlib.sha256(a.input.read_bytes()).hexdigest() != a.sha256:
            raise SystemExit('Archive checksum mismatch')
        validate_archive(a.input)
    elif not a.output or a.output.exists():
        raise SystemExit('Backup requires a new --output path; will not overwrite')
    if a.output:
        target = a.output.resolve()
        repo = Path(__file__).resolve().parents[1]
        if target == repo or repo in target.parents:
            raise SystemExit('Backup output must be outside the repository')
    name = 'wealthfolio-maint-' + uuid.uuid4().hex[:10]
    manifest = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': a.namespace, 'labels': {'app.kubernetes.io/managed-by': 'wealthfolio-maintenance'}}, 'spec': {'automountServiceAccountToken': False, 'restartPolicy': 'Never', 'securityContext': {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000, 'seccompProfile': {'type': 'RuntimeDefault'}}, 'containers': [{'name': 'maintenance', 'image': UTILITY, 'command': ['sh', '-c', 'sleep 3600'], 'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}, 'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'}, 'limits': {'memory': '128Mi'}}, 'volumeMounts': [{'name': 'data', 'mountPath': '/data', 'readOnly': a.operation == 'backup'}]}], 'volumes': [{'name': 'data', 'persistentVolumeClaim': {'claimName': a.claim, 'readOnly': a.operation == 'backup'}}]}}
    created = False
    try:
        run(scoped + ['create', '-f', '-'], input=json.dumps(manifest).encode())
        created = True
        run(scoped + ['wait', '--for=condition=Ready', 'pod/' + name, '--timeout=180s'])
        execute = scoped + ['exec', '-i', name, '--']
        if a.operation == 'backup':
            # O_EXCL and mode0600 avoid overwrite and exposure, including during a failed copy.
            fd = os.open(a.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as output:
                proc = subprocess.run(base + execute + ['tar', '-C', '/data', '-czf', '-', '.'], stdout=output, stderr=subprocess.PIPE)
                if proc.returncode:
                    raise RuntimeError('Archive failed; partial output retained privately; app remains stopped')
            validate_archive(a.output)
            digest = hashlib.sha256(a.output.read_bytes()).hexdigest()
            checksum = a.output.with_suffix(a.output.suffix + '.sha256')
            fd = os.open(checksum, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write(digest + '\n')
            print('PASS: stopped whole-data archive and checksum created; application remains stopped')
        else:
            run(execute + ['sh', '-c', 'test -z "$(find /data -mindepth 1 -maxdepth 1 -print -quit)"'])
            with a.input.open('rb') as archive:
                proc = subprocess.run(base + execute + ['tar', '-C', '/data', '-xzf', '-'], stdin=archive, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode:
                raise RuntimeError('Restore failed; application remains stopped; do not start partially restored data')
            print('PASS: archive restored to empty synthetic claim; application remains stopped pending readback')
    finally:
        if created:
            run(scoped + ['delete', 'pod', name, '--wait=true', '--timeout=120s'])


if __name__ == '__main__':
    main()
