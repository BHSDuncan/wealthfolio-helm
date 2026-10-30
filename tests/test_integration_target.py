"""Generic existing-cluster test configuration and mutation boundaries, offline."""
import argparse
import importlib.util
from pathlib import Path
import sys
from unittest import mock

import pytest

from test_render import render, resource

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def harness_modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'tests/integration'))
    modules = []
    for name in ('run', 'ingress'):
        spec = importlib.util.spec_from_file_location('integration_test_' + name, ROOT / 'tests/integration' / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    return modules


def test_existing_cluster_requires_full_identity_before_any_api_operation(harness_modules):
    runner, _ = harness_modules
    harness = runner.Harness.__new__(runner.Harness)
    harness.a = argparse.Namespace(environment='existing', kubeconfig=None, context=None,
                                   namespace=None, release=None, confirm_cluster_uid=None)
    harness.command = mock.Mock(side_effect=AssertionError('No API operation allowed'))
    with pytest.raises(RuntimeError, match='Existing cluster requires explicit'):
        harness.discover()
    harness.command.assert_not_called()


def test_existing_cluster_wrong_uid_refuses_mutation(harness_modules, tmp_path):
    runner, _ = harness_modules
    config = tmp_path / 'config'
    config.write_text('synthetic private config')
    harness = runner.Harness.__new__(runner.Harness)
    harness.a = argparse.Namespace(environment='existing', kubeconfig=str(config), context='chosen',
                                   namespace='wealthfolio-test', release='wealthfolio-test', confirm_cluster_uid='expected')
    harness.get = mock.Mock(return_value={'metadata': {'uid': 'different'}})
    harness.command = mock.Mock(side_effect=AssertionError('Mutation forbidden'))
    with pytest.raises(RuntimeError, match='Cluster identity mismatch'):
        harness.discover()
    harness.get.assert_called_once_with('namespace', 'kube-system')
    harness.command.assert_not_called()


def test_existing_cluster_cleanup_keeps_resources(harness_modules):
    runner, _ = harness_modules
    harness = runner.Harness.__new__(runner.Harness)
    harness.a = argparse.Namespace(environment='existing', keep_cluster=False)
    harness.processes = []
    harness.created_cluster = False
    harness.report = {}
    harness.results = [{'status': 'PASS'}]
    harness.save = mock.Mock()
    harness.command = mock.Mock(side_effect=AssertionError('No deletion allowed'))
    harness.close()
    assert 'retained' in harness.report['retention']
    harness.command.assert_not_called()


def test_existing_environment_is_supported_without_distribution_selection(harness_modules, monkeypatch):
    runner, _ = harness_modules
    monkeypatch.setattr(sys, 'argv', ['integration.sh', '--environment', 'existing'])
    assert runner.arguments().environment == 'existing'


@pytest.mark.parametrize('ingress_class,storage_class', [(None, None), ('edge-controller', 'durable-filesystem')])
def test_ingress_acceptance_values_render_controller_and_storage_choices(harness_modules, ingress_class, storage_class):
    _, ingress = harness_modules
    args = argparse.Namespace(release='wealthfolio-ingress-test', run_id='synthetic',
                              ingress_class=ingress_class, storage_class=storage_class)
    values = ingress.route_values(args, 'wealthfolio.example.test', 'https://wealthfolio.example.test:8443', 'auth', 'tls')
    docs = render(values)
    route = resource(docs, 'Ingress')
    claim = resource(docs, 'PersistentVolumeClaim')
    assert route['spec'].get('ingressClassName') == ingress_class
    assert not route['metadata'].get('annotations')
    assert claim['spec'].get('storageClassName') == storage_class
    assert route['spec']['tls'] == [{'secretName': 'tls', 'hosts': ['wealthfolio.example.test']}]


def test_ingress_acceptance_forwarding_uses_configured_destination_and_private_bind(harness_modules):
    _, ingress = harness_modules
    args = argparse.Namespace(local_port=8443, ingress_address='2001:db8::1', ingress_port=30443)
    assert ingress.forwarding(args) == '127.0.0.1:8443:[2001:db8::1]:30443'
