"""T-STATIC-01..06: behavioral chart contracts, including adversarial inputs."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'charts/wealthfolio'
HELM = os.environ.get('HELM_BIN', 'helm')
DIGEST = 'sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994'
REPOSITORY = 'ghcr.io/wealthfolio/wealthfolio'
TAG = '3.11.0'


def render(overrides=None, valid=True, bare=False, version='1.35.8'):
    values = {} if bare else {'auth': {'existingSecret': 'synthetic-auth-ref'}, 'image': {'repository': REPOSITORY, 'tag': TAG, 'digest': DIGEST}}
    def merge(target, source):
        for key, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)
    merge(values, overrides or {})
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml') as f:
        yaml.safe_dump(values, f)
        f.flush()
        result = subprocess.run([HELM, 'template', 'wealthfolio', str(CHART), '--namespace', 'chart-test', '--kube-version', version, '-f', f.name], capture_output=True, text=True)
    if valid:
        assert result.returncode == 0, result.stderr
        return [d for d in yaml.safe_load_all(result.stdout) if d]
    assert result.returncode != 0, 'Invalid configuration unexpectedly rendered'
    return result.stderr


def resource(docs, kind):
    return next(d for d in docs if d['kind'] == kind)


def pod(docs):
    return resource(docs, 'Deployment')['spec']['template']['spec']


def env(docs):
    return {e['name']: e for e in pod(docs)['containers'][0]['env']}


def test_default_resource_and_single_writer_contract():
    docs = render()
    assert {d['kind'] for d in docs} == {'Deployment', 'Service', 'PersistentVolumeClaim', 'Pod'}
    dep = resource(docs, 'Deployment')
    assert dep['spec']['replicas'] == 1
    assert dep['spec']['strategy'] == {'type': 'Recreate'}
    assert pod(docs)['automountServiceAccountToken'] is False
    container = pod(docs)['containers'][0]
    assert container['image'] == REPOSITORY + ':' + TAG + '@' + DIGEST
    assert container['ports'] == [{'name': 'http', 'containerPort': 8088, 'protocol': 'TCP'}]
    assert resource(docs, 'Service')['spec']['type'] == 'ClusterIP'
    assert resource(docs, 'Service')['spec']['ports'][0]['targetPort'] == 'http'
    assert resource(docs, 'PersistentVolumeClaim')['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'
    assert 'storageClassName' not in resource(docs, 'PersistentVolumeClaim')['spec']
    assert all(d['apiVersion'] in ('apps/v1', 'v1') for d in docs)


def test_official_image_defaults_require_only_an_external_secret_reference():
    docs = render({'auth': {'existingSecret': 'existing-auth'}}, bare=True)
    assert pod(docs)['containers'][0]['image'] == REPOSITORY + ':' + TAG + '@' + DIGEST
    assert env(docs)['WF_SECRET_KEY']['valueFrom']['secretKeyRef']['name'] == 'existing-auth'


def test_https_ingress_can_use_controller_certificate_without_secret_name():
    docs = render({'auth': {'cookieSecure': 'true'}, 'app': {'corsAllowOrigins': ['https://wealthfolio.example.test']}, 'ingress': {'enabled': True, 'className': 'traefik', 'hosts': ['wealthfolio.example.test'], 'tls': [{'hosts': ['wealthfolio.example.test']}]}})
    ingress = resource(docs, 'Ingress')['spec']
    assert ingress['tls'] == [{'hosts': ['wealthfolio.example.test']}]
    assert ingress['rules'][0]['http']['paths'][0]['path'] == '/'
    assert env(docs)['WF_COOKIE_SECURE']['value'] == 'true'


def test_credentials_and_fixed_environment():
    docs = render()
    values = env(docs)
    for name, key in [('WF_SECRET_KEY', 'WF_SECRET_KEY'), ('WF_AUTH_PASSWORD_HASH', 'WF_AUTH_PASSWORD_HASH')]:
        assert values[name]['valueFrom']['secretKeyRef'] == {'name': 'synthetic-auth-ref', 'key': key, 'optional': False}
        assert 'value' not in values[name]
    for name, expected in {'WF_LISTEN_ADDR': '0.0.0.0:8088', 'WF_DATA_DIR': '/data', 'WF_DB_PATH': '/data/wealthfolio.db', 'WF_AUTH_REQUIRED': 'true', 'WF_DB_REQUIRE_ENCRYPTION': 'false', 'WF_MCP_ENABLED': 'false', 'WF_MCP_AUDIT_ENABLED': 'true', 'WF_REQUEST_TIMEOUT_MS': '300000'}.items():
        assert values[name]['value'] == expected
    assert all(isinstance(v['value'], str) for v in values.values() if 'value' in v)
    text = yaml.safe_dump_all(docs)
    assert '$argon2' not in text and 'kind: Secret\n' not in text
    templates = '\n'.join(p.read_text() for p in (CHART / 'templates').rglob('*') if p.is_file())
    assert not any(token in templates for token in ('lookup ', 'randAlpha', 'tpl '))


def test_actual_security_mount_and_probe_fields():
    docs = render()
    p = pod(docs)
    assert p['securityContext'] == {'runAsNonRoot': True, 'runAsUser': 1000, 'runAsGroup': 1000, 'fsGroup': 1000, 'fsGroupChangePolicy': 'OnRootMismatch', 'seccompProfile': {'type': 'RuntimeDefault'}}
    c = p['containers'][0]
    assert c['securityContext'] == {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}}
    assert not p.get('initContainers') and not c.get('envFrom')
    mounts = {m['mountPath']: m for m in c['volumeMounts']}
    assert set(mounts) == {'/data', '/tmp'}
    assert all('subPath' not in m for m in mounts.values())
    assert next(v for v in p['volumes'] if v['name'] == mounts['/tmp']['name'])['emptyDir'] == {'medium': 'Memory', 'sizeLimit': '64Mi'}
    assert c['startupProbe']['httpGet']['path'] == '/api/v1/healthz'
    assert c['readinessProbe']['httpGet']['path'] == '/api/v1/readyz'
    assert c['livenessProbe']['httpGet']['path'] == '/api/v1/healthz'
    assert c['resources']['requests'] == {'cpu': '100m', 'memory': '256Mi'}
    assert c['resources']['limits'] == {'memory': '1Gi'}
    test_pod = resource(docs, 'Pod')
    assert test_pod['metadata']['annotations']['helm.sh/hook'] == 'test'
    assert 'hook-succeeded' in test_pod['metadata']['annotations']['helm.sh/hook-delete-policy']
    assert test_pod['spec']['automountServiceAccountToken'] is False
    assert test_pod['spec']['containers'][0]['image'].endswith('@sha256:3fb7e13e5d179a845c97d78204f3f1064d1907037f26a3ebf91710571151fd93')


@pytest.mark.parametrize('storage,expected', [(None, None), ('', ''), ('local-path', 'local-path')])
def test_storage_class_semantics(storage, expected):
    claim = resource(render({'persistence': {'storageClass': storage}}), 'PersistentVolumeClaim')['spec']
    if expected is None:
        assert 'storageClassName' not in claim
    else:
        assert claim['storageClassName'] == expected


def test_existing_claim_and_supported_placement():
    docs = render({'persistence': {'existingClaim': 'retained-data'}, 'service': {'port': 8090}, 'commonLabels': {'team': 'synthetic'}, 'podLabels': {'placement': 'lab'}, 'podAnnotations': {'test': 'yes'}, 'nodeSelector': {'kubernetes.io/arch': 'arm64'}, 'tolerations': [{'key': 'lab', 'operator': 'Exists', 'effect': 'NoSchedule'}]})
    assert not any(d['kind'] == 'PersistentVolumeClaim' for d in docs)
    assert any(v.get('persistentVolumeClaim', {}).get('claimName') == 'retained-data' for v in pod(docs)['volumes'])
    assert resource(docs, 'Service')['spec']['ports'][0]['port'] == 8090
    assert pod(docs)['nodeSelector'] == {'kubernetes.io/arch': 'arm64'}
    assert pod(docs)['tolerations'][0]['effect'] == 'NoSchedule'
    dep = resource(docs, 'Deployment')
    assert dep['spec']['selector']['matchLabels'].items() <= dep['spec']['template']['metadata']['labels'].items()
    assert dep['spec']['template']['metadata']['labels']['placement'] == 'lab'


def test_optional_ingress_mcp_encryption_and_env():
    docs = render({'ingress': {'enabled': True, 'className': 'traefik', 'hosts': ['wealthfolio.example.test'], 'tls': [{'secretName': 'existing-tls', 'hosts': ['wealthfolio.example.test']}]}, 'mcp': {'enabled': True, 'auditEnabled': False, 'allowedHosts': ['localhost:8088']}, 'encryption': {'required': True}, 'extraEnv': [{'name': 'RUST_LOG', 'value': 'warn'}, {'name': 'METAL_PRICE_API_KEY', 'valueFrom': {'secretKeyRef': {'name': 'provider-ref', 'key': 'api-key', 'optional': False}}}]})
    ingress = resource(docs, 'Ingress')['spec']
    assert ingress['ingressClassName'] == 'traefik'
    assert ingress['rules'][0]['http']['paths'][0]['path'] == '/'
    assert ingress['rules'][0]['http']['paths'][0]['pathType'] == 'Prefix'
    assert ingress['tls'][0]['secretName'] == 'existing-tls'
    e = env(docs)
    assert e['WF_MCP_ENABLED']['value'] == 'true'
    assert e['WF_MCP_AUDIT_ENABLED']['value'] == 'false'
    assert e['WF_MCP_ALLOWED_HOSTS']['value'] == 'localhost:8088'
    assert e['WF_DB_REQUIRE_ENCRYPTION']['value'] == 'true'
    assert e['RUST_LOG']['value'] == 'warn'
    assert e['METAL_PRICE_API_KEY']['valueFrom']['secretKeyRef']['key'] == 'api-key'


def test_digest_precedence_and_tag_only_mode():
    render({'image': {'tag': '3.11.1', 'digest': ''}}, valid=False)
    custom = 'sha256:' + 'a' * 64
    assert pod(render({'image': {'repository': 'example.test/reviewed/image', 'tag': '3.11.1', 'digest': custom}}))['containers'][0]['image'] == 'example.test/reviewed/image:3.11.1@' + custom


INVALID = [
    {'replicaCount': v} for v in (0, 2, '1')
] + [
    {'auth': {'existingSecret': ''}}, {'auth': {'secretKeys': {'secretKey': ''}}},
    {'auth': {'secretKeys': {'passwordHash': ''}}}, {'auth': {'cookieSecure': True}},
    {'auth': {'tokenTtlMinutes': 0}}, {'unknown': True}, {'persistence': {'enabled': False}},
    {'strategy': {'type': 'RollingUpdate'}}, {'image': {'tag': '3.11.1'}},
    {'image': {'digest': 'not-a-digest'}}, {'image': {'repository': ''}}, {'image': {'tag': ''}},
    {'image': {'repository': 'image:latest'}}, {'image': {'repository': 'image@sha256:abc'}},
    {'persistence': {'accessModes': []}}, {'persistence': {'accessModes': ['ReadWriteMany']}},
    {'persistence': {'accessModes': ['ReadWriteOnce', 'ReadWriteOncePod']}},
    {'persistence': {'size': '0Gi'}}, {'persistence': {'size': 'junk'}},
    {'persistence': {'annotations': {'helm.sh/resource-policy': 'delete'}}},
    {'commonLabels': {'app.kubernetes.io/instance': 'hijack'}},
    {'podLabels': {'app.kubernetes.io/name': 'hijack'}},
    {'podSecurityContext': {'runAsUser': 0}}, {'podSecurityContext': {'runAsGroup': 0}},
    {'podSecurityContext': {'fsGroup': 0}}, {'podSecurityContext': {'runAsNonRoot': False}},
    {'podSecurityContext': {'seccompProfile': {'type': 'Unconfined'}}},
    {'securityContext': {'allowPrivilegeEscalation': True}},
    {'securityContext': {'readOnlyRootFilesystem': False}},
    {'securityContext': {'capabilities': {'drop': []}}},
    {'securityContext': {'capabilities': {'add': ['SYS_ADMIN']}}},
    {'securityContext': {'privileged': True}}, {'service': {'port': 0}},
    {'service': {'nodePort': 30080}}, {'service': {'type': 'NodePort', 'nodePort': 0}},
    {'ingress': {'enabled': True}}, {'ingress': {'enabled': True, 'hosts': ['https://bad.test']}},
    {'ingress': {'enabled': True, 'hosts': ['good.test'], 'tls': [{'secretName': 'tls', 'hosts': ['other.test']}]}},
    {'mcp': {'enabled': True}}, {'mcp': {'enabled': True, 'allowedHosts': ['*']}},
    {'mcp': {'allowedHosts': ['https://host.test']}},
    {'extraEnv': [{'name': 'WF_AUTH_REQUIRED', 'value': 'false'}]},
    {'extraEnv': [{'name': 'WF_DATA_DIR', 'value': '/tmp'}]},
    {'extraEnv': [{'name': 'RUST_LOG', 'value': 'warn'}, {'name': 'RUST_LOG', 'value': 'debug'}]},
    {'extraEnv': [{'name': 'METAL_PRICE_API_KEY', 'value': 'forbidden-literal-sentinel'}]},
    {'extraEnv': [{'name': 'RUST_LOG', 'valueFrom': {'secretKeyRef': {'name': 'x', 'key': 'x'}}}]},
    {'extraEnv': [{'name': 'RUST_LOG', 'value': 'warn', 'valueFrom': {'secretKeyRef': {'name': 'x', 'key': 'x'}}}]},
    {'nameOverride': 'invalid_NAME'}, {'fullnameOverride': 'x' * 64},
    {'resources': {'limits': {'memory': 'garbage'}}}, {'tmp': {'sizeLimit': '0Mi'}},
    {'probes': {'startup': {'periodSeconds': 0}}},
] + [{'app': {'corsAllowOrigins': v}} for v in ([], ['*'], ['https://user:pass@host.test'], ['https://host.test/path'], ['https://host.test?q=x'], ['https://host.test#x'], ['http://localhost:8088', 'http://localhost:8088'])]


@pytest.mark.parametrize('values', INVALID)
def test_invalid_configurations_fail_before_resources(values):
    render(values, valid=False)


def test_bare_default_is_intentionally_undeployable():
    message = render(bare=True, valid=False)
    assert 'auth' in message


def test_all_examples_and_supported_kubernetes_versions():
    for example in sorted((ROOT / 'examples').glob('*.yaml')):
        render(yaml.safe_load(example.read_text()) or {})
    for version in ('1.34.9', '1.34.11', '1.35.8', '1.37.0'):
        render(version=version)
    render(version='1.33.0', valid=False)


def test_values_schema_contains_all_documented_defaults():
    defaults = yaml.safe_load((CHART / 'values.yaml').read_text())
    schema = json.loads((CHART / 'values.schema.json').read_text())
    assert schema['additionalProperties'] is False
    assert set(defaults) == set(schema['properties'])
