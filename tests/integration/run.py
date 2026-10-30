#!/usr/bin/env python3
"""Explicit-target synthetic Kubernetes lifecycle acceptance driver.

Reports are sanitized; private fixtures, archives and kubeconfig stay outside Git.
K3s never deletes a namespace, Secret or PVC, including after successful tests.
"""
import argparse
import base64
import contextlib
import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import tarfile
import time
import traceback
import uuid

import argon2
import yaml
from client import Client

ROOT = Path(__file__).resolve().parents[2]
if ROOT.name == 'tests':
    ROOT = ROOT.parent
IMAGE_DIGEST = 'sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994'
LABEL = 'wealthfolio.github.io/test-run'


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--environment', choices=['kind', 'k3s'], required=True)
    p.add_argument('--kubernetes-version', choices=['1.34.11', '1.35.8', '1.37.0'], default='1.35.8')
    p.add_argument('--helm-major', choices=['3', '4'], default='3')
    p.add_argument('--suite', choices=['full', 'smoke'], default='full')
    p.add_argument('--kubeconfig')
    p.add_argument('--context')
    p.add_argument('--namespace')
    p.add_argument('--release')
    p.add_argument('--values', type=Path)
    p.add_argument('--chart-package', type=Path, help='Immutable packaged chart to snapshot privately for this run')
    p.add_argument('--image-archive', type=Path, help='Import local OCI archive plus immutable digest alias into owned disposable Kind nodes')
    p.add_argument('--confirm-cluster-uid')
    p.add_argument('--resume-run')
    p.add_argument('--run-id', help='Owner label for a fresh run in a pre-created isolated namespace')
    p.add_argument('--state-dir', type=Path)
    p.add_argument('--report', type=Path)
    p.add_argument('--keep-cluster', action='store_true')
    p.add_argument('--skip-browser', action='store_true', help='Records mandatory T-BROWSER as NOT RUN; never a full pass')
    return p.parse_args()


class Harness:
    def __init__(self, a):
        self.a = a
        os.umask(0o077)
        self.state = a.state_dir or Path(tempfile.mkdtemp(prefix='wealthfolio-integration-'))
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.state.lstat()
        if self.state.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SystemExit('--state-dir must be an owned non-symlink mode0700 directory')
        if any(self.state.iterdir()) and not a.resume_run:
            raise SystemExit('Refusing nonempty --state-dir without explicit --resume-run')
        if ROOT == self.state.resolve() or ROOT in self.state.resolve().parents:
            raise SystemExit('--state-dir must be private and outside the repository')
        self.run_id = a.resume_run or a.run_id or ('wf-' + uuid.uuid4().hex[:12])
        self.results = []
        self.objects = []
        self.processes = []
        self.created_cluster = False
        self.prior_report = None
        if a.resume_run:
            report_path = a.report or self.state / 'report.json'
            self.prior_report = json.loads(report_path.read_text())
            if self.prior_report['runId'] != self.run_id:
                raise SystemExit('Private resume report/run ID mismatch')
        self.artifact = self.state / 'chart-package.tgz'
        if not self.artifact.exists():
            selected = a.chart_package or ROOT / 'dist/wealthfolio-0.1.0.tgz'
            data = selected.read_bytes()
            if self.prior_report and hashlib.sha256(data).hexdigest() != self.prior_report.get('packageSha256'):
                raise SystemExit('Resume package differs from original report; use original artifact or a fresh independent release')
            fd = os.open(self.artifact, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(data)
        if self.prior_report and hashlib.sha256(self.artifact.read_bytes()).hexdigest() != self.prior_report.get('packageSha256'):
            raise SystemExit('Private resume chart artifact checksum mismatch')
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            self.port = available.getsockname()[1]
        self.helm = 'helm4' if a.helm_major == '4' else 'helm'
        self.kubectl = 'kubectl'
        self.report = {'startedAt': stamp(), 'runId': self.run_id, 'environment': a.environment, 'suite': a.suite, 'helmMajor': a.helm_major, 'checkId': ('k3s' if a.environment == 'k3s' else ('helm4-smoke' if a.helm_major == '4' else 'kind-' + a.kubernetes_version + '-' + a.suite)), 'source': self.source(), 'chartVersion': '0.1.0', 'appVersion': '3.11.0', 'imageDigest': IMAGE_DIGEST, 'results': self.results}
        self.report['packageSha256'] = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        if self.prior_report:
            raw = json.dumps(self.prior_report, sort_keys=True).encode()
            (self.state / ('prior-report-' + hashlib.sha256(raw).hexdigest()[:12] + '.json')).write_bytes(raw)
            retained = [r for r in self.prior_report['results'] if r['status'] == 'PASS' and r['testId'] != 'T-TARGET']
            self.report['resumedFrom'] = {'reportSha256': hashlib.sha256(raw).hexdigest(), 'source': self.prior_report['source'], 'packageSha256': self.prior_report.get('packageSha256'), 'imageDigest': self.prior_report.get('imageDigest'), 'retainedCaseIds': [r['testId'] for r in retained], 'reason': 'Continue only after explicit target/UID inventory and original private fixture recovery; retained assertions keep their original source/package origin'}
            for previous in retained:
                row = copy.deepcopy(previous)
                row.setdefault('evidenceOrigin', {'source': self.prior_report['source'], 'packageSha256': self.prior_report.get('packageSha256'), 'imageDigest': self.prior_report.get('imageDigest')})
                self.results.append(row)
            self.objects.extend(self.prior_report.get('objects', []))

    def source(self):
        sha = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True)
        dirty = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True)
        files = sorted(p for folder in ['charts', 'scripts', 'tests'] for p in (ROOT / folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
        tree = hashlib.sha256()
        for path in files:
            tree.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes())
        return {'revision': sha.stdout.strip() if sha.returncode == 0 else 'UNBORN', 'dirty': bool(dirty.stdout.strip()) or dirty.returncode != 0, 'implementationTreeSha256': tree.hexdigest()}

    def command(self, args, input=None, check=True, timeout=700):
        result = subprocess.run([str(x) for x in args], cwd=ROOT, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if check and result.returncode:
            # No command/response dump: args may contain private paths; output may contain auth values.
            executable = Path(str(args[0])).name
            (self.state / ('private-' + executable + '-failure.stderr')).write_bytes(result.stderr)
            raise RuntimeError(f'{executable} operation returned {result.returncode}; raw output withheld')
        return result

    def kube(self, args, **kwargs):
        return self.command([self.kubectl, '--kubeconfig', self.a.kubeconfig, '--context', self.a.context, '-n', self.a.namespace] + args, **kwargs)

    def get(self, resource, name=None, labels=None):
        args = ['get', resource] + ([name] if name else []) + (['-l', labels] if labels else []) + ['-o', 'json']
        return json.loads(self.kube(args).stdout)

    def helm_cmd(self, args, **kwargs):
        return self.command([self.helm] + args + ['--kubeconfig', self.a.kubeconfig, '--kube-context', self.a.context, '--namespace', self.a.namespace], **kwargs)

    def result(self, test_id, status, detail):
        self.results.append({'testId': test_id, 'status': status, 'at': stamp(), 'assertions': detail, 'evidenceOrigin': {'source': self.report['source'], 'packageSha256': self.report.get('packageSha256'), 'imageDigest': self.report.get('imageDigest')}})
        print(f'{test_id}: {status} — {detail}', flush=True)
        self.save()

    def passed(self, test_id):
        return any(r['testId'] == test_id and r['status'] == 'PASS' for r in self.results)

    def save(self):
        self.report['updatedAt'] = stamp()
        self.report['objects'] = self.objects
        path = self.a.report or self.state / 'report.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.report, indent=2) + '\n')

    def create(self, manifest):
        result = json.loads(self.kube(['create', '-f', '-', '-o', 'json'], input=json.dumps(manifest).encode()).stdout)
        self.objects.append({'kind': result['kind'], 'name': result['metadata']['name'], 'uid': result['metadata']['uid']})
        return result

    def discover(self):
        a = self.a
        if a.environment == 'k3s' or a.kubeconfig:
            if not all((a.kubeconfig, a.context, a.namespace, a.release, a.confirm_cluster_uid)):
                raise RuntimeError('Existing cluster requires explicit kubeconfig/context/namespace/release/confirm-cluster-uid')
        else:
            name = self.run_id
            a.kubeconfig = str(self.state / 'kind.kubeconfig')
            a.context = 'kind-' + name
            a.namespace = a.release = self.run_id
            lock = json.loads((ROOT / 'tools.lock.json').read_text())
            self.command(['kind', 'create', 'cluster', '--name', name, '--image', lock['kindNodes'][a.kubernetes_version], '--kubeconfig', a.kubeconfig, '--wait', '180s'])
            self.created_cluster = True
        if not Path(a.kubeconfig).is_file():
            raise RuntimeError('Explicit kubeconfig is missing')
        for value in (a.namespace, a.release):
            if not re.fullmatch(r'[a-z0-9]([-a-z0-9]*[a-z0-9])?', value) or value in ('default', 'kube-system', 'kube-public'):
                raise RuntimeError('Refusing unsafe target name')
        if a.environment == 'kind' and not a.context.startswith('kind-wealthfolio-') and not a.context.startswith('kind-wf-'):
            raise RuntimeError('Kind driver accepts only explicitly test-named contexts')
        cluster_uid = self.get('namespace', 'kube-system')['metadata']['uid']
        if a.confirm_cluster_uid and a.confirm_cluster_uid != cluster_uid:
            raise RuntimeError('Cluster identity mismatch; no mutation performed')
        self.cluster_uid = cluster_uid
        version = json.loads(self.kube(['version', '-o', 'json']).stdout)['serverVersion']
        major, minor = int(version['major']), int(re.sub(r'\D', '', version['minor']))
        if (major, minor) < (1, 34):
            raise RuntimeError('Kubernetes below owner-approved1.34 floor; refusing unsupported target')
        if minor >= 37:
            self.kubectl = 'kubectl137'
            if not shutil.which(self.kubectl):
                raise RuntimeError('Missing locked kubectl1.37; run scripts/setup-tools.py')
        os.environ['KUBECTL_BIN'] = self.kubectl
        nodes = self.get('nodes')['items']
        self.report['kubernetesVersion'] = version['gitVersion']
        self.report['architectures'] = sorted({n['status']['nodeInfo']['architecture'] for n in nodes})
        self.report['helmVersion'] = self.command([self.helm, 'version', '--short']).stdout.decode().strip()
        self.report['kubectlVersion'] = json.loads(self.command([self.kubectl, 'version', '--client', '-o', 'json']).stdout)['clientVersion']['gitVersion']
        self.report['target'] = {'clusterUid': cluster_uid, 'namespace': a.namespace, 'release': a.release}
        exists = self.kube(['get', 'namespace', a.namespace, '-o', 'json'], check=False)
        if exists.returncode == 0:
            namespace = json.loads(exists.stdout)
            if namespace['metadata'].get('labels', {}).get(LABEL) != self.run_id:
                raise RuntimeError('Namespace belongs to another owner; no mutation performed')
            # Resume needs original private password/key/fixture and stage-aware implementation.
            if a.resume_run:
                assert self.prior_report['target'] == {'clusterUid': cluster_uid, 'namespace': a.namespace, 'release': a.release}, 'Resume target differs from private inventory'
                for item in self.objects:
                    if item['kind'] == 'PersistentVolumeClaim':
                        actual = self.get('pvc', item['name'])
                        assert actual['metadata']['uid'] == item['uid'], 'Resume claim UID mismatch'
            else:
                for resource in ('deployments', 'pods', 'services', 'persistentvolumeclaims', 'secrets'):
                    if self.get(resource)['items']:
                        raise RuntimeError('Fresh run refuses nonempty namespace; use new isolated target and preserve prior data')
        else:
            self.create({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': a.namespace, 'labels': {LABEL: self.run_id}}})
        self.result('T-TARGET', 'PASS', 'Explicit test target identity/version/namespace ownership checked before application mutations')

    def import_image(self):
        if not self.a.image_archive:
            return
        if self.a.environment != 'kind':
            raise RuntimeError('K3s import is an owner-performed containerd operation, never elevated by this harness')
        if not self.a.values:
            raise RuntimeError('--image-archive requires --values with explicit repository/tag/digest')
        image = yaml.safe_load(self.a.values.read_text())['image']
        with tarfile.open(self.a.image_archive) as archive:
            index = json.load(archive.extractfile('index.json'))
        assert index['manifests'][0]['digest'] == image['digest'], 'OCI index and values digest disagree'
        names = self.command(['kind', 'get', 'nodes', '--name', self.a.context.removeprefix('kind-')]).stdout.decode().splitlines()
        assert names and all(n.startswith(self.a.context.removeprefix('kind-')) for n in names)
        for node in names:
            with self.a.image_archive.open('rb') as archive:
                proc = subprocess.run(['docker', 'exec', '-i', node, 'ctr', '--namespace', 'k8s.io', 'images', 'import', '--all-platforms', '-'], stdin=archive, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert proc.returncode == 0, 'Owned Kind node OCI import failed'
            self.command(['docker', 'exec', node, 'ctr', '--namespace', 'k8s.io', 'images', 'tag', '--force', image['repository'] + ':' + image['tag'], image['repository'] + '@' + image['digest']])
        self.report['imageDigest'] = image['digest']
        self.report['imageArchiveSha256'] = hashlib.sha256(self.a.image_archive.read_bytes()).hexdigest()

    def bootstrap(self):
        self.password = secrets.token_urlsafe(24)
        self.master = base64.b64encode(secrets.token_bytes(32)).decode()
        phc = argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, type=argon2.Type.ID).hash(self.password)
        self.secret_name = self.a.release + '-auth'
        self.create({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': self.secret_name, 'namespace': self.a.namespace, 'labels': {LABEL: self.run_id}}, 'type': 'Opaque', 'stringData': {'WF_SECRET_KEY': self.master, 'WF_AUTH_PASSWORD_HASH': phc}})
        private = {'password': self.password, 'masterKey': self.master}
        (self.state / 'recovery.json').write_text(json.dumps(private))
        self.values = {'auth': {'existingSecret': self.secret_name}, 'fullnameOverride': self.a.release, 'commonLabels': {LABEL: self.run_id}}
        self.values['app'] = {'corsAllowOrigins': ['http://localhost:8088', 'http://127.0.0.1:8088', 'http://localhost:' + str(self.port)]}
        if self.a.values:
            external = yaml.safe_load(self.a.values.read_text()) or {}
            if external.get('persistence', {}).get('existingClaim'):
                raise RuntimeError('Fresh synthetic integration must never seed an existing claim')
            # Local values may choose discovered storage/origin/placement; never credentials.
            if external.get('auth', {}).get('existingSecret') not in (None, '', 'wealthfolio-auth', self.secret_name):
                raise RuntimeError('Harness creates its own synthetic Secret; values may not select unrelated credentials')
            for key, value in external.items():
                if key not in ('auth', 'fullnameOverride', 'commonLabels'):
                    self.values[key] = value
            origins = self.values.setdefault('app', {}).setdefault('corsAllowOrigins', [])
            if 'http://localhost:' + str(self.port) not in origins:
                origins.append('http://localhost:' + str(self.port))
        self.write_values()
        if 'image' in self.values:
            self.report['imageDigest'] = self.values['image']['digest']

    def write_values(self):
        self.values_file = self.state / 'values.yaml'
        self.values_file.write_text(yaml.safe_dump(self.values))

    def install(self, release=None, values_file=None):
        release = release or self.a.release
        artifact = self.artifact
        if not artifact.is_file():
            raise RuntimeError('Run scripts/package.sh first; integration installs packaged artifact')
        self.report['packageSha256'] = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.helm_cmd(['install', release, str(artifact), '-f', str(values_file or self.values_file), '--wait', '--timeout', '10m'])
        self.ready(release)

    def ready(self, release=None):
        release = release or self.a.release
        self.kube(['rollout', 'status', 'deployment/' + release, '--timeout=600s'])
        dep = self.get('deployment', release)
        assert dep['spec']['replicas'] == dep['status']['readyReplicas'] == 1
        pods = self.get('pods', labels='app.kubernetes.io/instance=' + release)['items']
        actual = [p for p in pods if not p['metadata'].get('deletionTimestamp') and p['status'].get('phase') == 'Running']
        assert len(actual) == 1
        self.pod_name = actual[0]['metadata']['name']
        self.report['resolvedImageId'] = actual[0]['status']['containerStatuses'][0]['imageID']
        return actual[0]

    @contextlib.contextmanager
    def client(self, release=None):
        release = release or self.a.release
        port = self.port
        args = [self.kubectl, '--kubeconfig', self.a.kubeconfig, '--context', self.a.context, '-n', self.a.namespace, 'port-forward', 'service/' + release, str(port) + ':8088', '--address', '127.0.0.1']
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(proc)
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError('Loopback port-forward failed')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    time.sleep(.1)
            yield Client('http://localhost:' + str(port), self.password)
        finally:
            proc.terminate()
            proc.wait(timeout=10)

    def security(self):
        self.ready()
        script = 'test "$(id -u)" = 1000 && test "$(id -g)" = 1000 && touch /data/.chart-write-test && touch /tmp/.chart-write-test && ! touch /chart-must-fail 2>/dev/null && test ! -e /var/run/secrets/kubernetes.io/serviceaccount/token && grep -q "CapEff:[[:space:]]*0000000000000000" /proc/self/status && grep -q "NoNewPrivs:[[:space:]]*1" /proc/self/status'
        self.kube(['exec', self.pod_name, '--', 'sh', '-c', script])
        self.kube(['exec', self.pod_name, '--', 'sh', '-c', 'mkdir -p /data/addons/chart-test && printf full-tree-sentinel > /data/addons/chart-test/sentinel'])
        self.result('T-SEC', 'PASS', 'Runtime UID/GID1000, writes data/tmp, root write denied, no API token, no effective caps, no-new-privileges')

    def assert_data_files(self):
        self.kube(['exec', self.pod_name, '--', 'sh', '-c', 'test "$(cat /data/addons/chart-test/sentinel)" = full-tree-sentinel'])
        # Markers pass through protected stdin; never arguments/logs. Files remain encrypted.
        script = b'set -eu\nfind /data -type f -name "*secret*" -print | grep -q .\n'
        for item in self.fixture:
            script += ('set +e\ngrep -R -F -q -e ' + "'" + item['marker'] + "'" + ' /data\nrc=$?\nset -e\ntest "$rc" = 1\n').encode()
        self.kube(['exec', '-i', self.pod_name, '--', 'sh'], input=script)

    def verify(self, release=None, encrypted=False):
        self.ready(release)
        with self.client(release) as client:
            client.verify(self.fixture, encrypted)
        self.assert_data_files()

    def stop(self):
        self.set_replicas(0)
        self.kube(['wait', '--for=delete', 'pod', '-l', 'app.kubernetes.io/instance=' + self.a.release, '--timeout=180s'])
        assert self.get('pods', labels='app.kubernetes.io/instance=' + self.a.release)['items'] == []

    def restart(self):
        self.stop()
        self.set_replicas(1)
        self.verify()

    def set_replicas(self, count):
        # Patch the permitted parent resource; no deployments/scale RBAC needed.
        self.kube(['patch', 'deployment', self.a.release, '--type=merge', '-p', json.dumps({'spec': {'replicas': count}})])

    def upgrade(self):
        self.write_values()
        self.helm_cmd(['upgrade', self.a.release, str(self.artifact), '-f', str(self.values_file), '--wait', '--timeout', '10m'])
        self.ready()

    def archive_database_digests(self, path):
        digests = {}
        with tarfile.open(path) as archive:
            for member in archive.getmembers():
                if member.isfile() and (member.name.endswith('.db') or member.name.endswith('.db.encrypted')):
                    digests[member.name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
        assert len(digests) >= 2, 'Whole-tree archive must contain both profile databases'
        return digests

    def expect_policy_failure(self, reason, before_archive):
        self.write_values()
        self.helm_cmd(['upgrade', self.a.release, str(self.artifact), '-f', str(self.values_file), '--wait', '--timeout', '10m'])
        # Profiles are lazy: shallow health may stay200 while opening either DB fails.
        self.ready()
        with self.client() as client:
            client.login()
            for item in self.fixture:
                body, _ = client.request('POST', '/api/v1/profiles/unlock_profile', {'profileId': item['profile']}, expected=500)
                assert isinstance(body, str) and body.startswith('PROFILE_STARTUP_FAILED:') and reason.lower() in body.lower(), 'Expected encrypted-policy error; body withheld'
        self.stop()
        after = self.state / ('post-policy-mismatch-' + uuid.uuid4().hex[:8] + '.tgz')
        self.archive_stopped(after)
        # A stopped process can leave committed WAL pages. Opening/checking the
        # database can checkpoint those pages even when serving is denied.
        before_hashes = self.archive_database_digests(before_archive)
        after_hashes = self.archive_database_digests(after)
        assert set(before_hashes) == set(after_hashes), 'Policy mismatch changed database path identity'
        changed = sum(before_hashes[name] != after_hashes[name] for name in before_hashes)
        self.report.setdefault('observations', []).append({'testId': 'T-ENC', 'primaryDatabaseByteChanges': changed, 'explanation': 'Stopped whole-tree archive retains committed WAL; profile-open can checkpoint it before policy rejection. Logical fixture is verified under the original policy before conversion.'})
        target_policy = self.values['encryption']['required']
        self.values['encryption']['required'] = not target_policy
        self.upgrade()
        self.verify(encrypted=not target_policy)
        self.stop()
        self.values['encryption']['required'] = target_policy

    def expect_wrong_key_failure(self):
        self.write_values()
        self.helm_cmd(['upgrade', self.a.release, str(self.artifact), '-f', str(self.values_file)])
        for _ in range(90):
            pods = self.get('pods', labels='app.kubernetes.io/instance=' + self.a.release)['items']
            for pod in pods:
                if pod['metadata'].get('deletionTimestamp'):
                    continue
                statuses = pod['status'].get('containerStatuses', [])
                if not statuses:
                    continue
                status = statuses[0]
                terminated = status['state'].get('terminated') or status.get('lastState', {}).get('terminated')
                if terminated and terminated['exitCode'] != 0:
                    logs = self.kube(['logs', pod['metadata']['name'], '--tail=80'], check=False).stdout.decode(errors='replace').lower()
                    assert any(term in logs for term in ('decrypt', 'vault', 'key', 'not a database')), 'Wrong-key startup error category not observed; logs withheld'
                    self.stop()
                    return
                if status.get('ready'):
                    with self.client() as client:
                        client.login()
                        for item in self.fixture:
                            body, _ = client.request('POST', '/api/v1/profiles/unlock_profile', {'profileId': item['profile']}, expected=500)
                            assert isinstance(body, str) and body.startswith('PROFILE_STARTUP_FAILED:') and any(term in body.lower() for term in ('decrypt', 'key', 'not a database')), 'Wrong-key profile-open failure not observed; body withheld'
                    self.stop()
                    return
            time.sleep(1)
        raise AssertionError('Wrong-key fixture neither failed startup nor denied profile access within bounded timeout')

    def offline_convert(self, operation):
        assert operation in ('encrypt', 'decrypt')
        assert self.get('deployment', self.a.release)['spec']['replicas'] == 0
        assert self.get('pods', labels='app.kubernetes.io/instance=' + self.a.release)['items'] == []
        # Same app image, UID/GID, key and paths; one stopped profile at a time.
        source = self.get('deployment', self.a.release)['spec']['template']['spec']
        for item in self.fixture:
            name = 'wf-db-' + uuid.uuid4().hex[:10]
            spec = copy.deepcopy(source)
            spec['restartPolicy'] = 'Never'
            container = spec['containers'][0]
            for key in ('startupProbe', 'readinessProbe', 'livenessProbe', 'ports'):
                container.pop(key, None)
            container['command'] = ['/usr/local/bin/wealthfolio-server']
            container['args'] = ['db', operation, '--profile', item['profile']]
            self.create({'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': self.a.namespace, 'labels': {LABEL: self.run_id}}, 'spec': spec})
            try:
                for _ in range(180):
                    phase = self.get('pod', name)['status']['phase']
                    if phase == 'Succeeded':
                        break
                    if phase == 'Failed':
                        raise AssertionError('Offline per-profile conversion failed (logs withheld)')
                    time.sleep(1)
                else:
                    raise AssertionError('Offline per-profile conversion timed out')
            finally:
                self.kube(['delete', 'pod', name, '--wait=true', '--timeout=120s'])

    def archive_stopped(self, output):
        self.command(['bash', 'scripts/backup.sh', '--kubeconfig', self.a.kubeconfig, '--context', self.a.context, '--namespace', self.a.namespace, '--release', self.a.release, '--claim', self.claim, '--confirm-cluster-uid', self.cluster_uid, '--confirm-stopped', '--output', output])

    def encryption(self):
        original = (self.a.release, self.values, self.fixture, self.claim)
        try:
            self.a.release = original[0] + '-enc-' + uuid.uuid4().hex[:6]
            self.values = {'auth': {'existingSecret': self.secret_name}, 'fullnameOverride': self.a.release, 'commonLabels': {LABEL: self.run_id}, 'app': copy.deepcopy(original[1]['app']), 'encryption': {'required': True}}
            if 'image' in original[1]:
                self.values['image'] = copy.deepcopy(original[1]['image'])
            if 'persistence' in original[1]:
                self.values['persistence'] = {k: v for k, v in original[1]['persistence'].items() if k != 'existingClaim'}
            self.write_values()
            self.install()
            self.claim = self.a.release
            self.objects.append({'kind': 'PersistentVolumeClaim', 'name': self.claim, 'uid': self.get('pvc', self.claim)['metadata']['uid']})
            with self.client() as client:
                self.fixture = client.seed(encrypted=True)
            (self.state / (self.a.release + '-private-fixture.json')).write_text(json.dumps({'fixture': self.fixture, 'values': self.values, 'claim': self.claim}))
            self.security()
            self.verify(encrypted=True)
            self.result('T-ENC-FRESH', 'PASS', 'Fresh encrypted claim opened two profiles; actual API encryption=true and full account/vault readback')
            self.stop()
            before_decrypt = self.state / ('pre-decrypt-' + uuid.uuid4().hex[:8] + '.tgz')
            self.archive_stopped(before_decrypt)
            self.values['encryption']['required'] = False
            self.expect_policy_failure('encrypted but WF_DB_REQUIRE_ENCRYPTION', before_decrypt)
            self.offline_convert('decrypt')
            self.upgrade()
            self.verify(encrypted=False)
            self.result('T-ENC-DECRYPT', 'PASS', 'Encrypted+false mismatch failed closed; stopped backed-up per-profile decrypt recovered complete plaintext fixture')
            self.stop()
            before_encrypt = self.state / ('pre-encrypt-' + uuid.uuid4().hex[:8] + '.tgz')
            self.archive_stopped(before_encrypt)
            self.values['encryption']['required'] = True
            self.expect_policy_failure('not encrypted', before_encrypt)
            self.offline_convert('encrypt')
            self.upgrade()
            self.verify(encrypted=True)
            self.result('T-ENC-ENCRYPT', 'PASS', 'Plaintext+true mismatch failed closed; stopped backed-up per-profile encrypt recovered complete encrypted fixture')
            self.stop()
            wrong = self.secret_name + '-wrong-' + uuid.uuid4().hex[:6]
            good = self.get('secret', self.secret_name)
            self.create({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': wrong, 'namespace': self.a.namespace, 'labels': {LABEL: self.run_id}}, 'type': 'Opaque', 'data': {'WF_SECRET_KEY': base64.b64encode(base64.b64encode(secrets.token_bytes(32))).decode(), 'WF_AUTH_PASSWORD_HASH': good['data']['WF_AUTH_PASSWORD_HASH']}})
            self.values['auth']['existingSecret'] = wrong
            self.expect_wrong_key_failure()
            self.values['auth']['existingSecret'] = self.secret_name
            self.upgrade()
            self.verify(encrypted=True)
            self.helm_cmd(['uninstall', self.a.release, '--wait', '--timeout', '180s'])
            self.result('T-ENC', 'PASS', 'Fresh encrypted fixture actual enabled=true; both policy mismatches fail closed; stopped backed-up per-profile decrypt/encrypt preserves accounts/vault; wrong key fails and original key recovers')
        finally:
            self.a.release, self.values, self.fixture, self.claim = original
            self.write_values()

    def negative_secrets(self):
        original = (self.a.release, self.values)
        try:
            for variant in ('missing-secret', 'missing-key', 'bad-master', 'bad-phc'):
                release = original[0] + '-' + variant
                secret = release + '-auth'
                if variant != 'missing-secret':
                    valid_phc = argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1, type=argon2.Type.ID).hash(self.password)
                    data = {'WF_SECRET_KEY': self.master, 'WF_AUTH_PASSWORD_HASH': valid_phc}
                    if variant == 'missing-key':
                        data.pop('WF_SECRET_KEY')
                    elif variant == 'bad-master':
                        data['WF_SECRET_KEY'] = 'not-valid-base64'
                    elif variant == 'bad-phc':
                        data['WF_AUTH_PASSWORD_HASH'] = 'not-a-phc'
                    self.create({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': secret, 'namespace': self.a.namespace, 'labels': {LABEL: self.run_id}}, 'type': 'Opaque', 'stringData': data})
                self.a.release = release
                self.values = {'auth': {'existingSecret': secret}, 'fullnameOverride': release, 'commonLabels': {LABEL: self.run_id}}
                if 'image' in original[1]:
                    self.values['image'] = copy.deepcopy(original[1]['image'])
                self.write_values()
                self.helm_cmd(['install', release, str(self.artifact), '-f', str(self.values_file)])
                observed = False
                for _ in range(120):
                    pods = self.get('pods', labels='app.kubernetes.io/instance=' + release)['items']
                    for pod in pods:
                        status = pod['status'].get('containerStatuses', [])
                        if not status:
                            continue
                        state = status[0]['state']
                        if variant.startswith('missing'):
                            observed = state.get('waiting', {}).get('reason') == 'CreateContainerConfigError'
                        else:
                            logs = self.kube(['logs', pod['metadata']['name'], '--tail=80'], check=False).stdout.decode(errors='replace')
                            observed = not status[0]['ready'] and any(term in logs.lower() for term in ('invalid', 'failed', 'error', 'panic'))
                        if observed:
                            break
                    if observed:
                        break
                    time.sleep(1)
                assert observed, 'Expected Secret fail-closed startup not observed: ' + variant
                self.helm_cmd(['uninstall', release, '--wait', '--timeout', '180s'])
            self.result('T-AUTH-NEGATIVE', 'PASS', 'Separate fresh claims reject missing Secret/key at kubelet and malformed master key/PHC at application startup')
        finally:
            self.a.release, self.values = original
            self.write_values()

    def backup(self):
        self.stop()
        args = ['--kubeconfig', self.a.kubeconfig, '--context', self.a.context, '--namespace', self.a.namespace, '--release', self.a.release, '--confirm-cluster-uid', self.cluster_uid, '--confirm-stopped']
        archive = self.state / ('whole-data-' + uuid.uuid4().hex[:8] + '.tgz')
        self.command(['bash', 'scripts/backup.sh'] + args + ['--claim', self.claim, '--output', archive])
        restored = self.a.release + '-restore-' + uuid.uuid4().hex[:6]
        storage = self.get('pvc', self.claim)['spec'].get('storageClassName')
        spec = {'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '5Gi'}}}
        if storage is not None:
            spec['storageClassName'] = storage
        self.create({'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': restored, 'namespace': self.a.namespace, 'labels': {LABEL: self.run_id}}, 'spec': spec})
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.command(['bash', 'scripts/restore.sh'] + args + ['--claim', restored, '--input', archive, '--sha256', digest, '--confirm-empty-test-claim'])
        restore_values = dict(self.values)
        restore_values['fullnameOverride'] = restored
        restore_values['persistence'] = {'existingClaim': restored}
        restore_file = self.state / 'restore-values.yaml'
        restore_file.write_text(yaml.safe_dump(restore_values))
        self.install(restored, restore_file)
        self.verify(restored)
        self.helm_cmd(['uninstall', restored, '--wait', '--timeout', '180s'])
        self.set_replicas(1)
        self.verify()
        self.result('T-BACKUP', 'PASS', 'Stopped whole-tree archive/checksum restored to new empty synthetic claim; two profiles/accounts/vaults read back with original key')

    def retention(self):
        uid = self.get('pvc', self.claim)['metadata']['uid']
        self.helm_cmd(['uninstall', self.a.release, '--wait', '--timeout', '180s'])
        assert self.get('pvc', self.claim)['metadata']['uid'] == uid
        assert self.get('pvc', self.claim)['status']['phase'] == 'Bound'
        assert self.get('pvc', self.claim)['metadata'].get('labels', {}).get(LABEL) == self.run_id, 'Fresh claim ownership must match before synthetic seeding'
        self.get('secret', self.secret_name)
        assert self.kube(['get', 'deployment', self.a.release], check=False).returncode != 0
        assert self.kube(['get', 'service', self.a.release], check=False).returncode != 0
        self.values['persistence'] = {'existingClaim': self.claim}
        self.write_values()
        self.install()
        self.verify()
        assert self.get('pvc', self.claim)['metadata']['uid'] == uid
        self.result('T-RETAIN', 'PASS', 'Uninstall retained exact Bound PVC UID and external Secret; existingClaim reinstall recovered full fixture')

    def run(self):
        self.discover()
        self.import_image()
        if self.a.resume_run:
            recovery = json.loads((self.state / 'recovery.json').read_text())
            self.password, self.master = recovery['password'], recovery['masterKey']
            self.fixture = json.loads((self.state / 'fixture.json').read_text())
            self.values = yaml.safe_load((self.state / 'values.yaml').read_text())
            self.values_file = self.state / 'values.yaml'
            self.secret_name = self.values['auth']['existingSecret']
            self.port = int(next(o.rsplit(':', 1)[1] for o in self.values['app']['corsAllowOrigins'] if o.startswith('http://localhost:') and o != 'http://localhost:8088'))
            self.report['imageDigest'] = self.values['image']['digest']
            self.report['packageSha256'] = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
            # Existing fixture is always read back; never reseeded or credentials regenerated.
            self.ready()
            with self.client() as client:
                client.verify(self.fixture, encrypted=False)
            self.result('T-PROBES', 'PASS', 'Both public healthz/readyz returned HTTP200 with exact ok bodies; original owned fixture read back')
        else:
            self.bootstrap()
            self.install()
        self.claim = next(v['persistentVolumeClaim']['claimName'] for v in self.get('deployment', self.a.release)['spec']['template']['spec']['volumes'] if 'persistentVolumeClaim' in v)
        assert self.get('pvc', self.claim)['status']['phase'] == 'Bound'
        self.objects.append({'kind': 'PersistentVolumeClaim', 'name': self.claim, 'uid': self.get('pvc', self.claim)['metadata']['uid']})
        if not self.a.resume_run:
            self.helm_cmd(['test', self.a.release, '--timeout', '120s'])
            assert self.get('ingress', labels='app.kubernetes.io/instance=' + self.a.release)['items'] == []
            self.result('T-INSTALL', 'PASS', 'Packaged chart installed, one ready replica, bound claim, health hook passed, no Ingress')
            self.security()
            with self.client() as client:
                client.network()
                self.fixture = client.seed()
            (self.state / 'fixture.json').write_text(json.dumps(self.fixture))
            self.assert_data_files()
            self.result('T-AUTH', 'PASS', 'No-session and wrong-password401; literal-dollar Argon2id login/session HttpOnly cookie; two-profile authenticated data/vault fixture')
            self.result('T-NET', 'PASS', 'Loopback-only port-forward, root HTML, exact allowed CORS credentials, disallowed origin not permitted, MCP disabled status and POST405 static fallback')
        if not self.passed('T-DATA'):
            self.restart()
            self.result('T-DATA', 'PASS', 'Stopped old writer before restart; exact two-profile/account/vault/sentinel readback and stable claim')
        if self.a.suite == 'smoke':
            self.result('T-HELM4' if self.a.helm_major == '4' else 'T-SMOKE', 'PASS', 'Fresh independent packaged release auth/security/data smoke completed')
            return
        if not self.passed('T-UPGRADE'):
            self.values['podAnnotations'] = {'chart-test-stage': 'upgrade'}
            self.upgrade()
            self.verify()
            self.result('T-UPGRADE', 'PASS', 'Same-image same-storage chart revision with harmless annotation upgrade recovered complete fixture')
        if not self.passed('T-ROLLBACK'):
            self.helm_cmd(['rollback', self.a.release, '1', '--wait', '--timeout', '10m'])
            self.verify()
            self.result('T-ROLLBACK', 'PASS', 'Same-image revision rollback retained claim/key and complete fixture; no app downgrade claimed')
        if not self.passed('T-BACKUP'):
            self.backup()
        if not self.passed('T-RETAIN'):
            self.retention()
        if not self.passed('T-ENC'):
            self.encryption()
        if not self.passed('T-MCP'):
            self.values['mcp'] = {'enabled': True, 'auditEnabled': True, 'allowedHosts': ['localhost', '127.0.0.1']}
            self.upgrade()
            with self.client() as client:
                client.mcp(self.fixture)
            self.values['mcp']['auditEnabled'] = False
            self.upgrade()
            with self.client() as client:
                client.mcp(self.fixture, audit=False)
            self.result('T-MCP', 'PASS', 'Protocol2025-11-25 initialize/list/read, scope denial, profile isolation, wrong/missing Host, revoked/absent PAT and audit on/off verified')
        if not self.passed('T-AUTH-NEGATIVE'):
            self.negative_secrets()
        if self.a.skip_browser:
            self.result('T-BROWSER', 'NOT RUN', 'Explicit --skip-browser; mandatory browser gate remains open')
        else:
            with self.client() as client:
                browser = self.state / 'browser.json'
                browser.write_text(json.dumps({'password': self.password, 'accountName': self.fixture[0]['name'], 'profileId': self.fixture[0]['profile'], 'accountId': self.fixture[0]['account'], 'vaultSha256': hashlib.sha256(self.fixture[0]['marker'].encode()).hexdigest()}))
                self.command(['node', 'tests/integration/browser.mjs', '--base-url', client.url, '--state-file', browser], timeout=120)
            self.result('T-BROWSER', 'PASS', 'Actual frontend login/account visible/logout protected data')

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        self.report['finishedAt'] = stamp()
        self.report['status'] = 'PASS' if self.results and all(r['status'] == 'PASS' for r in self.results) else 'INCOMPLETE'
        self.report['retention'] = 'Synthetic namespace/Secret/PVC retained; final deletion requires owner choice' if self.a.environment == 'k3s' or not self.created_cluster or self.a.keep_cluster else 'Owned disposable Kind cluster removed'
        self.save()
        if self.created_cluster and not self.a.keep_cluster:
            self.command(['kind', 'delete', 'cluster', '--name', self.run_id], check=False)


def main():
    harness = Harness(arguments())
    try:
        harness.run()
    except Exception as error:
        (harness.state / 'failure-stack.txt').write_text(traceback.format_exc())
        harness.result('T-HARNESS', 'FAIL', str(error))
        harness.close()
        return 1
    harness.close()
    return 0 if harness.report['status'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
