"""T-STATIC-04/06: reject bypasses at nested values boundaries before deployment."""
import copy
import json

import pytest
import yaml

from test_render import CHART, env, pod, render, resource


def nested_unknowns():
    """Exercise every fixed object that exists in defaults, including security children."""
    schema = json.loads((CHART / 'values.schema.json').read_text())
    defaults = yaml.safe_load((CHART / 'values.yaml').read_text())
    cases = []

    def walk(node, values, path):
        if node.get('type') != 'object' or node.get('additionalProperties') is not False:
            return
        override = {'unsupportedChartBypass': 'noncredential-sentinel'}
        for key in reversed(path):
            override = {key: override}
        cases.append(pytest.param(override, id='unknown-' + ('.'.join(path) or 'root')))
        for key, child in node.get('properties', {}).items():
            if isinstance(values, dict) and isinstance(values.get(key), dict):
                walk(child, values[key], path + [key])

    walk(schema, defaults, [])
    return cases


@pytest.mark.parametrize('override', nested_unknowns())
def test_unknown_fixed_object_field_is_rejected(override):
    message = render(override, valid=False)
    assert 'unsupportedChartBypass' in message
    assert 'noncredential-sentinel' not in message


@pytest.mark.parametrize('override', [
    {'auth': {'existingSecret': 'bad..name'}},
    {'auth': {'existingSecret': 'a.' + 'b' * 64}},
    {'auth': {'secretKeys': {'secretKey': 'invalid/key'}}},
    {'auth': {'tokenTtlMinutes': -1}},
    {'auth': {'tokenTtlMinutes': 525601}},
    {'persistence': {'existingClaim': 'bad..name'}},
    {'persistence': {'storageClass': 'bad..name'}},
    {'app': {'corsAllowOrigins': ['http://bad..name']}},
    {'app': {'corsAllowOrigins': ['http://[:::]']}},
    {'app': {'corsAllowOrigins': ['http://[12345::1]']}},
    {'app': {'corsAllowOrigins': ['http://-bad.name']}},
    {'app': {'corsAllowOrigins': ['http://localhost:99999']}},
    {'app': {'corsAllowOrigins': ['http://localhost:0']}},
    {'app': {'corsAllowOrigins': ['http://localhost:8088/']}},
    {'app': {'corsAllowOrigins': ['ftp://localhost']}},
    {'mcp': {'enabled': True, 'allowedHosts': ['localhost:99999']}},
    {'mcp': {'allowedHosts': ['localhost:0']}},
    {'mcp': {'allowedHosts': ['[:::]:8088']}},
    {'mcp': {'allowedHosts': ['[12345::1]']}},
    {'mcp': {'allowedHosts': ['[::1]:65536']}},
    {'mcp': {'allowedHosts': ['::1']}},
    {'app': {'listenAddress': '127.0.0.1'}},
    {'app': {'listenAddress': '[::]:8088'}},
    {'mcp': {'allowedHosts': ['host.test/path']}},
    {'mcp': {'allowedHosts': ['host.test', 'host.test']}},
    {'ingress': {'enabled': True, 'hosts': ['*.host.test']}},
    {'ingress': {'enabled': True, 'hosts': ['host.test'], 'tls': [{'secretName': '', 'hosts': ['host.test']}]}},
    {'service': {'type': 'LoadBalancer', 'nodePort': 30080}},
    {'service': {'type': 'NodePort', 'nodePort': 65536}},
    {'service': {'port': 65536}},
    {'tests': {'image': {'tag': 'different-tag'}}},
    {'tests': {'image': {'digest': 'sha256:' + 'A' * 64}}},
    {'imagePullSecrets': [{'name': 'pull', 'value': 'unsupported'}]},
    {'commonLabels': {'helm.sh/chart': 'override'}},
    {'commonLabels': {'meta.helm.sh/release-name': 'override'}},
    {'podLabels': {'app.kubernetes.io/managed-by': 'override'}},
    {'podAnnotations': {'meta.helm.sh/release-namespace': 'override'}},
    {'service': {'annotations': {'helm.sh/hook': 'post-install'}}},
    {'ingress': {'annotations': {'meta.helm.sh/release-name': 'override'}}},
    {'commonLabels': {'bad key': 'x'}},
    {'podLabels': {'team': 'invalid value'}},
    {'service': {'annotations': {'controller.test/key': True}}},
    {'securityContext': {'capabilities': {'drop': ['ALL', 'NET_ADMIN']}}},
    {'podSecurityContext': {'sysctls': [{'name': 'net.ipv4.ip_forward', 'value': '1'}]}},
    {'resources': {'requests': {'cpu': '0'}}},
    {'probes': {'liveness': {'timeoutSeconds': -1}}},
    {'extraEnv': [{'name': 'METAL_PRICE_API_KEY', 'valueFrom': {'secretKeyRef': {'name': 'provider', 'key': 'api-key', 'optional': True}}}]},
    {'extraEnv': [{'name': 'METAL_PRICE_API_KEY', 'valueFrom': {'fieldRef': {'fieldPath': 'metadata.name'}}}]},
    {'extraEnv': [{'name': 'RUST_LOG', 'value': ''}]},
    {'extraEnv': [{'name': 'RUST_LOG', 'value': 'warn', 'envFrom': []}]},
])
def test_adversarial_invalid_values(override):
    render(override, valid=False)


@pytest.mark.parametrize('name', [
    'WF_AUTH_PASSWORD_HASH', 'WF_SECRET_KEY', 'WF_SECRET_KEY_FILE', 'WF_SECRET_FILE',
    'WF_AUTH_REQUIRED', 'WF_DATA_DIR', 'WF_DB_PATH', 'WF_ADDONS_DIR', 'WF_LISTEN_ADDR',
    'WF_CORS_ALLOW_ORIGINS', 'WF_DB_REQUIRE_ENCRYPTION', 'WF_MCP_ENABLED',
    'WF_MCP_AUDIT_ENABLED', 'WF_MCP_ALLOWED_HOSTS', 'WF_OIDC_ISSUER_URL',
    'WF_OIDC_CLIENT_ID', 'WF_REQUEST_TIMEOUT_MS',
])
def test_managed_environment_cannot_be_overridden(name):
    render({'extraEnv': [{'name': name, 'value': 'noncredential-sentinel'}]}, valid=False)


def test_custom_image_and_metadata_do_not_change_selectors_or_claim():
    base = render()
    changed = render({'image': {'tag': 'reviewed-app', 'digest': 'sha256:' + 'b' * 64},
                      'commonLabels': {'team': 'synthetic'}, 'podLabels': {'placement': 'lab'},
                      'podAnnotations': {'example.test/note': '{{ must-remain-literal }}'}})
    assert resource(base, 'Deployment')['spec']['selector'] == resource(changed, 'Deployment')['spec']['selector']
    assert resource(base, 'Service')['spec']['selector'] == resource(changed, 'Service')['spec']['selector']
    assert pod(base)['volumes'][0] == pod(changed)['volumes'][0]
    assert resource(changed, 'Deployment')['spec']['template']['metadata']['annotations']['example.test/note'] == '{{ must-remain-literal }}'
    assert env(base)['WF_SECRET_KEY'] == env(changed)['WF_SECRET_KEY']


def test_test_image_tag_only_mode_and_literal_annotations():
    docs = render({'tests': {'image': {'tag': 'reviewed-test', 'digest': ''}},
                   'service': {'annotations': {'example.test/note': '{{ must-remain-literal }}'}}})
    assert resource(docs, 'Pod')['spec']['containers'][0]['image'].endswith(':reviewed-test')
    assert resource(docs, 'Service')['metadata']['annotations']['example.test/note'] == '{{ must-remain-literal }}'


def test_valid_ipv6_origin_uses_exact_bracketed_authority():
    assert env(render({'app': {'corsAllowOrigins': ['http://[::1]:8088']}}))['WF_CORS_ALLOW_ORIGINS']['value'] == 'http://[::1]:8088'


def test_ipv6_listener_and_mcp_hosts_keep_port_and_origin_contracts():
    docs = render({'app': {'listenAddress': '::', 'corsAllowOrigins': ['https://[2001:db8::1]:8443']},
                   'mcp': {'enabled': True, 'allowedHosts': ['[2001:db8::1]:8443', '[::1]']}})
    values = env(docs)
    assert values['WF_LISTEN_ADDR']['value'] == '[::]:8088'
    assert values['WF_CORS_ALLOW_ORIGINS']['value'] == 'https://[2001:db8::1]:8443'
    assert values['WF_MCP_ALLOWED_HOSTS']['value'] == '[2001:db8::1]:8443,[::1]'
    assert pod(docs)['containers'][0]['ports'][0]['containerPort'] == 8088


@pytest.mark.parametrize('node_port', [20080, 32768])
def test_custom_cluster_node_port_ranges_are_not_restricted_to_default(node_port):
    docs = render({'service': {'type': 'NodePort', 'nodePort': node_port}})
    assert resource(docs, 'Service')['spec']['ports'][0]['nodePort'] == node_port


def test_legacy_values_without_listener_keep_ipv4_default():
    assert env(render({'app': {'listenAddress': None}}))['WF_LISTEN_ADDR']['value'] == '0.0.0.0:8088'


def test_ingress_notes_use_exact_browser_origin_with_external_port(tmp_path):
    import shutil
    import subprocess
    from test_render import HELM

    chart = tmp_path / 'chart'
    shutil.copytree(CHART, chart)
    notes = chart / 'templates/NOTES.txt'
    original = notes.read_text()
    notes.unlink()
    (chart / 'templates/_preview-notes.tpl').write_text('{{ define "preview-notes" }}' + original + '{{ end }}')
    (chart / 'templates/preview.yaml').write_text('apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: preview\ndata:\n  notes: |\n{{ include "preview-notes" . | indent 4 }}\n')
    values = tmp_path / 'values.yaml'
    values.write_text(yaml.safe_dump({'auth': {'existingSecret': 'auth'},
                                     'ingress': {'enabled': True, 'hosts': ['wealthfolio.example.test'],
                                                 'tls': [{'hosts': ['wealthfolio.example.test']}]},
                                     'app': {'corsAllowOrigins': ['https://wealthfolio.example.test:8443']}}))
    rendered = subprocess.check_output([HELM, 'template', 'wealthfolio', str(chart), '--kube-version', '1.35.8',
                                        '--show-only', 'templates/preview.yaml', '--values', str(values)], text=True)
    text = yaml.safe_load(rendered)['data']['notes']
    assert '  https://wealthfolio.example.test:8443\n' in text
    assert '  https://wealthfolio.example.test\n' not in text
