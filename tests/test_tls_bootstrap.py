"""Certificate ownership, real CA/leaf cryptography and bounded issuer contracts."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import threading

import pytest

spec = importlib.util.spec_from_file_location("tls_bootstrap", Path(__file__).parents[1] / "scripts/tls_bootstrap.py")
tls = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tls)


class FakeInstaller:
    def __init__(self, state):
        self.state = state
        self.namespace = "wealthfolio"
        self.uid, self.gid = os.getuid(), os.getgid()
        self.record = {"installationId": "a" * 32, "values": {"image": "unchanged", "key": "reference-only"}}
        self.objects = {}
        self.operations = []
        self.lose_patch_response = False
        self.issuer_ready = True
        self.issue_secret = True
        self.patch_paths = []
        self.stale_generation = False
        self.secure_directory(state)

    def secure_directory(self, path):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert not path.is_symlink()
        path.chmod(0o700)
        return path

    def private_file(self, path):
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and stat.S_IMODE(info.st_mode) == 0o600

    def write_private(self, path, text, replace=False):
        if path.exists():
            self.private_file(path)
            assert replace
        path.write_text(text)
        path.chmod(0o600)

    def object(self, kind, name, namespaced=True):
        if kind in {"issuer.cert-manager.io", "clusterissuer.cert-manager.io"}:
            return {"metadata": {"uid": "issuer-uid"}, "status": {"conditions": [{"type": "Ready", "status": "True" if self.issuer_ready else "False"}]}}
        value = copy.deepcopy(self.objects.get((kind, name)))
        if kind == "secret" and value:
            return {"metadata": value["metadata"]}  # Never return key contents.
        return value

    def kube(self, *args, data=None, **kwargs):
        self.operations.append((args, data))
        if args[0] == "create":
            value = json.loads(data)
            value["metadata"]["uid"] = value["metadata"]["name"] + "-uid"
            if value["kind"] == "Secret":
                value["data"] = {k: base64.b64encode(v.encode()).decode() for k, v in value.pop("stringData").items()}
                self.objects[("secret", value["metadata"]["name"])] = value
            elif value["kind"] == "Certificate":
                self.objects[("certificate.cert-manager.io", value["metadata"]["name"])] = value
                # Emulate an API default that must not break idempotent retries.
                value["spec"]["revisionHistoryLimit"] = 1
                value["metadata"]["generation"] = 2
                value["status"] = {"conditions": [{"type": "Ready", "status": "True", "observedGeneration": 1 if self.stale_generation else 2}]}
                if self.issue_secret:
                    template = copy.deepcopy(value["spec"]["secretTemplate"])
                    template["annotations"].update({"cert-manager.io/certificate-name": value["metadata"]["name"], "cert-manager.io/issuer-name": value["spec"]["issuerRef"]["name"], "cert-manager.io/issuer-kind": value["spec"]["issuerRef"]["kind"]})
                    self.objects[("secret", value["spec"]["secretName"])] = {"metadata": {"uid": "issued-secret-uid", **copy.deepcopy(template)}}
            else:
                raise AssertionError("Unexpected controller/global-resource creation")
        elif args[0] == "patch":
            assert args[:2] == ("patch", "secret") and "--patch-file" in args
            patch_file = Path(args[args.index("--patch-file") + 1])
            assert patch_file.is_file() and patch_file.stat().st_mode & 0o777 == 0o600
            assert data is None and patch_file != Path("-")
            self.patch_paths.append(patch_file)
            data = patch_file.read_text()
            self.operations[-1] = (args, data)
            value = self.objects[("secret", args[2])]
            for row in json.loads(data):
                parts = [part.replace("~1", "/").replace("~0", "~") for part in row["path"].strip("/").split("/")]
                target = value
                for part in parts[:-1]:
                    target = target[part]
                if row["op"] == "test":
                    assert target[parts[-1]] == row["value"]
                else:
                    assert row["op"] == "replace"
                    target[parts[-1]] = row["value"]
            if self.lose_patch_response:
                self.lose_patch_response = False
                raise RuntimeError("simulated lost response after atomic patch")
        else:
            assert args[0] == "wait" and "--timeout=300s" in args
        return None


@pytest.fixture
def installer(tmp_path):
    return FakeInstaller(tmp_path / "private")


def provision(installer, **kwargs):
    return tls.provision(installer, "private-ca", "wealthfolio.example.test", "wealthfolio-private-tls", **kwargs)


def test_controller_and_existing_secret_do_not_generate_keys_or_write_resources(installer):
    assert tls.provision(installer, "controller", "wealthfolio.example.test", None)["secretName"] is None
    installer.objects[("secret", "already-issued")] = {"metadata": {"uid": "external-uid"}}
    result = tls.provision(installer, "secret", "wealthfolio.example.test", "already-issued")
    assert result["secretUid"] == "external-uid" and not installer.operations
    assert not list(installer.state.rglob("*.pem"))


def test_private_ca_real_chain_san_secure_files_and_idempotent_reuse(installer):
    original = copy.deepcopy(installer.record)
    first = provision(installer)
    again = provision(installer)
    assert first == again and installer.record == original
    assert len(installer.operations) == 1
    public = Path(first["publicCaFile"])
    assert b"PRIVATE KEY" not in public.read_bytes()
    assert first["publicCaSha256"] == hashlib.sha256(public.read_bytes()).hexdigest()
    for path in installer.state.rglob("*.pem"):
        installer.private_file(path)
    item = installer.objects[("secret", first["secretName"])]
    assert base64.b64decode(item["data"]["tls.key"]) != next(installer.state.rglob("ca-key.pem")).read_bytes()
    leaf = next(installer.state.rglob("tls-certificate.pem"))
    tls._openssl("verify", "-x509_strict", "-verify_hostname", "wealthfolio.example.test", "-CAfile", public, leaf)
    with pytest.raises(tls.TLSSetupError):
        tls._openssl("verify", "-verify_hostname", "wrong.example.test", "-CAfile", public, leaf)


def test_generated_private_ca_works_in_actual_verified_tls_handshake(installer):
    result = provision(installer)
    key = next(installer.state.rglob("tls-key.pem"))
    cert = next(installer.state.rglob("tls-certificate.pem"))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_context = ssl.create_default_context(cafile=result["publicCaFile"])
    server, client = socket.socketpair()
    errors = []
    def serve():
        try:
            with server_context.wrap_socket(server, server_side=True) as secure:
                assert secure.recv(2) == b"hi"
                secure.sendall(b"ok")
        except Exception as error:
            errors.append(error)
    worker = threading.Thread(target=serve)
    worker.start()
    with client_context.wrap_socket(client, server_hostname="wealthfolio.example.test") as secure:
        secure.sendall(b"hi")
        assert secure.recv(2) == b"ok"
    worker.join(timeout=10)
    assert not worker.is_alive() and not errors


def test_explicit_renewal_keeps_ca_and_secret_uid_and_changes_only_leaf(installer):
    first = provision(installer)
    before = copy.deepcopy(installer.objects[("secret", first["secretName"])])
    second = provision(installer, renew=True)
    after = installer.objects[("secret", second["secretName"])]
    assert first["publicCaSha256"] == second["publicCaSha256"]
    assert first["secretUid"] == second["secretUid"]
    assert before["data"] != after["data"]
    patch = json.loads(installer.operations[-1][1])
    assert patch[0] == {"op": "test", "path": "/metadata/uid", "value": first["secretUid"]}
    assert patch[1]["op"] == "test" and tls.FINGERPRINT.replace("/", "~1") in patch[1]["path"]
    assert all(not path.exists() for path in installer.patch_paths)


def test_lost_renewal_response_reuses_pending_leaf_without_second_patch(installer):
    first = provision(installer)
    installer.lose_patch_response = True
    with pytest.raises(RuntimeError, match="lost response"):
        provision(installer, renew=True)
    pending = json.loads(next(installer.state.rglob("secret-*.json")).read_text())["pending"]
    second = provision(installer, renew=True)
    assert second["secretUid"] == first["secretUid"]
    assert len([item for item in installer.operations if item[0][0] == "patch"]) == 1
    state = json.loads(next(installer.state.rglob("secret-*.json")).read_text())
    assert state["active"] == pending and "pending" not in state
    assert all(not path.exists() for path in installer.patch_paths)


def test_unknown_or_replaced_secret_is_never_overwritten(installer):
    installer.objects[("secret", "wealthfolio-private-tls")] = {"metadata": {"uid": "unknown", "labels": {}}}
    with pytest.raises(tls.TLSSetupError, match="outside this certificate"):
        provision(installer)
    assert not installer.operations and not list(installer.state.rglob("*key.pem"))
    installer.objects.clear()
    result = provision(installer)
    installer.objects[("secret", result["secretName"])]["metadata"]["uid"] = "replacement"
    with pytest.raises(tls.TLSSetupError, match="replaced"):
        provision(installer, renew=True)
    assert not [item for item in installer.operations if item[0][0] == "patch"]


def test_name_injection_fails_before_openssl_or_api_operations(installer):
    with pytest.raises(tls.TLSSetupError, match="DNS"):
        tls.provision(installer, "private-ca", "host\n[evil]", "valid-name")
    assert not installer.operations


def test_managed_issuer_contract_is_namespaced_bounded_and_retry_safe(installer):
    first = tls.provision(installer, "cert-manager", "wealthfolio.example.test", "wealthfolio-managed-tls", "ready-issuer")
    second = tls.provision(installer, "cert-manager", "wealthfolio.example.test", "wealthfolio-managed-tls", "ready-issuer")
    assert first == second and first["publicCaFile"] is None
    creates = [json.loads(data) for args, data in installer.operations if args[0] == "create"]
    assert len(creates) == 1 and creates[0]["kind"] == "Certificate"
    assert creates[0]["spec"]["issuerRef"] == {"name": "ready-issuer", "kind": "Issuer", "group": "cert-manager.io"}
    assert creates[0]["spec"]["dnsNames"] == ["wealthfolio.example.test"]
    assert all("--timeout=300s" in args for args, _ in installer.operations if args[0] == "wait")


def test_managed_setup_refuses_not_ready_or_unowned_existing_certificate(installer):
    installer.issuer_ready = False
    with pytest.raises(tls.TLSSetupError, match="not Ready"):
        tls.provision(installer, "cert-manager", "wealthfolio.example.test", "wealthfolio-managed-tls", "issuer")
    assert not installer.operations
    installer.issuer_ready = True
    installer.objects[("certificate.cert-manager.io", "wealthfolio-managed-tls")] = {"metadata": {"uid": "other", "labels": {}}, "spec": {}}
    with pytest.raises(tls.TLSSetupError, match="unowned"):
        tls.provision(installer, "cert-manager", "wealthfolio.example.test", "wealthfolio-managed-tls", "issuer")
    assert not installer.operations


def test_managed_ready_must_match_current_certificate_generation(installer):
    installer.stale_generation = True
    with pytest.raises(tls.TLSSetupError, match="current owned request generation"):
        tls.provision(installer, "cert-manager", "wealthfolio.example.test", "wealthfolio-managed-tls", "issuer")
    assert installer.record["values"] == {"image": "unchanged", "key": "reference-only"}


def setup_script(tmp_path, *, crd=True):
    """Fake API binaries record any real-create attempts; no cluster is contacted."""
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "api-calls.jsonl"
    kubectl = binary / "kubectl"
    kubectl.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
args=sys.argv[1:]
with open(os.environ['WF_SETUP_TEST_LOG'],'a') as log:log.write(json.dumps(args)+'\\n')
if 'current-context' in args:
    if os.environ.get('WF_SETUP_TEST_CONTEXT_ERROR')=='yes':raise SystemExit('synthetic unreadable configuration')
    print(os.environ.get('WF_SETUP_TEST_CONTEXT','current-test'))
elif 'version' in args:print(json.dumps({'serverVersion':{'gitVersion':os.environ.get('WF_SETUP_TEST_VERSION','v1.35.8')}}))
elif 'get' in args:
    kind=args[args.index('get')+1]
    if kind=='customresourcedefinition' and os.environ['WF_SETUP_TEST_CRD']=='yes':print(json.dumps({'metadata':{'uid':'existing-crd'}}))
    elif kind=='namespace':print(json.dumps({'metadata':{'uid':'existing-namespace'}}))
elif '--dry-run=client' in args:print(pathlib.Path(args[args.index('-f')+1]).read_text())
elif 'create' in args:
    value=json.load(sys.stdin)
    with open(os.environ['WF_SETUP_TEST_CREATED'],'a') as output:output.write(json.dumps(value)+'\\n')
elif 'wait' not in args:raise SystemExit('Unexpected test operation')
''')
    kubectl.chmod(0o700)
    config = tmp_path / "test.kubeconfig"
    config.write_text("synthetic client settings")
    environment = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                       KUBECONFIG=str(config),
                       WF_SETUP_TEST_LOG=str(log), WF_SETUP_TEST_CRD="yes" if crd else "no",
                       WF_SETUP_TEST_CREATED=str(tmp_path / "created.jsonl"))
    return environment, log, tmp_path / "created.jsonl"


def call_setup(environment, *args, explicit_target=True):
    import subprocess
    script = Path(__file__).parents[1] / "scripts/setup-cert-manager.sh"
    target = ["--context", "explicit-test"] if explicit_target else []
    return subprocess.run(["bash", str(script), *target, "--namespace", "wealthfolio", "--issuer", "chosen-issuer", *args], env=environment, capture_output=True, text=True)


def test_bootstrap_dry_run_has_no_api_contact_and_declares_global_scope(tmp_path):
    environment, log, created = setup_script(tmp_path)
    result = call_setup(environment, "--email", "operator@example.test", "--dry-run", "--install-controller")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["globalControllerInstall"] is True
    assert plan["issuer"]["kind"] == "Issuer"
    assert plan["issuer"]["spec"]["acme"]["solvers"] == [{"http01": {"ingress": {}}}]
    assert not log.exists() and not created.exists()


def test_cert_manager_accepts_vendor_build_suffix_without_changing_context_or_ingress_class(tmp_path):
    environment, log, created = setup_script(tmp_path)
    environment['WF_SETUP_TEST_VERSION'] = 'v1.35.8+vendor.1'
    result = call_setup(environment, "--email", "operator@example.test", explicit_target=False)
    assert result.returncode == 0, result.stderr
    assert "context current-test" in result.stdout
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[0] == ['config', 'current-context']
    for command in commands[1:]:
        assert command[command.index('--context') + 1] == 'current-test'
    document = json.loads(created.read_text().strip())
    assert document['spec']['acme']['solvers'] == [{'http01': {'ingress': {}}}]


def test_cert_manager_kubeconfig_alone_and_class_override_are_honored(tmp_path):
    environment, log, created = setup_script(tmp_path)
    config = tmp_path / 'custom cluster.yaml'
    config.write_text('synthetic selected config')
    result = call_setup(environment, '--kubeconfig', str(config), '--ingress-class', 'nginx',
                        '--email', 'operator@example.test', explicit_target=False)
    assert result.returncode == 0, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    for command in commands:
        assert command[command.index('--kubeconfig') + 1] == str(config)
    document = json.loads(created.read_text().strip())
    assert document['spec']['acme']['solvers'] == [{'http01': {'ingress': {'ingressClassName': 'nginx'}}}]


def test_cert_manager_broken_config_stops_before_any_api_operation(tmp_path):
    environment, log, created = setup_script(tmp_path)
    environment['WF_SETUP_TEST_CONTEXT_ERROR'] = 'yes'
    result = call_setup(environment, '--email', 'operator@example.test', explicit_target=False)
    assert result.returncode != 0 and 'No usable current Kubernetes context' in result.stderr
    assert [json.loads(line) for line in log.read_text().splitlines()] == [['config', 'current-context']]
    assert not created.exists()


@pytest.mark.parametrize('target,expected_ingress', [([], {}), (['--context', 'preview'], {}),
                                                    (['--kubeconfig', '/unavailable/preview-config'], {}),
                                                    (['--ingress-class', 'nginx'], {'ingressClassName': 'nginx'})])
def test_cert_manager_dry_run_needs_no_client_binaries_or_configuration(tmp_path, target, expected_ingress):
    import shutil, subprocess, sys
    binary = tmp_path / 'bin'
    binary.mkdir()
    (binary / 'python3').symlink_to(sys.executable)
    (binary / 'dirname').symlink_to(shutil.which('dirname'))
    environment = dict(os.environ, PATH=str(binary), KUBECONFIG=str(tmp_path / 'missing-config'))
    script = Path(__file__).parents[1] / 'scripts/setup-cert-manager.sh'
    result = subprocess.run([shutil.which('bash'), str(script), *target, '--namespace', 'wealthfolio', '--issuer', 'chosen-issuer',
                             '--email', 'operator@example.test', '--dry-run'],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert 'deferred until execution' in plan['targetResolution']
    assert 'HTTP01' in plan['prerequisites']
    ingress = plan['issuer']['spec']['acme']['solvers'][0]['http01']['ingress']
    assert ingress == expected_ingress


def test_bootstrap_accepts_explicit_context_and_kubeconfig_without_api_contact(tmp_path):
    environment, log, created = setup_script(tmp_path)
    result = call_setup(environment, "--kubeconfig", str(tmp_path / "custom cluster.yaml"),
                        "--email", "operator@example.test", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["target"] == "explicit-test"
    assert not log.exists() and not created.exists()


def test_missing_controller_never_installs_without_explicit_opt_in(tmp_path):
    environment, _, created = setup_script(tmp_path, crd=False)
    result = call_setup(environment, "--email", "operator@example.test")
    assert result.returncode != 0 and "--install-controller" in result.stderr
    assert not created.exists()


def test_controller_opt_in_refuses_existing_operator_adoption(tmp_path):
    environment, _, created = setup_script(tmp_path)
    result = call_setup(environment, "--email", "operator@example.test", "--install-controller")
    assert result.returncode != 0 and "already exist" in result.stderr
    assert not created.exists()


@pytest.mark.parametrize("kind", ["ClusterIssuer", "List", "hidden-list", "wrong-namespace"])
def test_provider_issuer_file_cannot_create_cluster_objects_or_lists(tmp_path, kind):
    environment, _, created = setup_script(tmp_path)
    document = {"apiVersion": "cert-manager.io/v1", "kind": "Issuer", "metadata": {"name": "chosen-issuer", "namespace": "wealthfolio"}, "spec": {"ca": {"secretName": "existing-ca-ref"}}}
    if kind == "wrong-namespace":document["metadata"]["namespace"] = "different"
    elif kind == "hidden-list":document["items"] = [{"kind": "ClusterIssuer"}]
    else:document["kind"] = kind
    file = tmp_path / "reviewed-issuer.json"
    file.write_text(json.dumps(document))
    file.chmod(0o600)
    result = call_setup(environment, "--issuer-file", str(file))
    assert result.returncode != 0 and "single" not in result.stdout
    assert "exactly one namespaced" in result.stderr
    assert not created.exists()


def test_reviewed_provider_issuer_file_is_applied_without_credentials_or_operator_guessing(tmp_path):
    environment, log, created = setup_script(tmp_path)
    document = {"apiVersion": "cert-manager.io/v1", "kind": "Issuer", "metadata": {"name": "chosen-issuer", "namespace": "wealthfolio"}, "spec": {"acme": {"server": "https://acme-v02.api.letsencrypt.org/directory", "privateKeySecretRef": {"name": "account-reference"}, "solvers": [{"dns01": {"cloudflare": {"apiTokenSecretRef": {"name": "provider-token-reference", "key": "token"}}}}]}}}
    file = tmp_path / "reviewed-dns01.json"
    file.write_text(json.dumps(document))
    file.chmod(0o600)
    result = call_setup(environment, "--issuer-file", str(file))
    assert result.returncode == 0, result.stderr
    assert json.loads(created.read_text()) == document
    assert "provider-token-reference" not in result.stdout + result.stderr
    assert not any('helm' in row for row in log.read_text().splitlines())


def test_rejected_provider_file_is_checked_before_explicit_controller_install(tmp_path):
    environment, log, created = setup_script(tmp_path, crd=False)
    file = tmp_path / "not-an-issuer.json"
    file.write_text(json.dumps({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "chosen-issuer", "namespace": "wealthfolio"}, "spec": {"ignored": True}}))
    file.chmod(0o600)
    result = call_setup(environment, "--issuer-file", str(file), "--install-controller")
    assert result.returncode != 0 and "before any cluster action" in result.stderr
    assert not log.exists() and not created.exists()


def test_actual_kubectl_accepts_private_patch_filename_offline(tmp_path):
    import shutil,subprocess
    binary=Path(__file__).parents[1]/".tools/bin/kubectl"
    if not binary.is_file():
        found=shutil.which("kubectl")
        if not found:pytest.skip("Run scripts/setup-tools.py for the real kubectl compatibility check")
        binary=Path(found)
    base=tmp_path/"secret.json"
    base.write_text(json.dumps({"apiVersion":"v1","kind":"Secret","metadata":{"name":"tls-local-check","namespace":"wealthfolio","uid":"owned-uid"},"data":{"tls.key":base64.b64encode(b"old-synthetic-key").decode()}}))
    patch=tmp_path/"private-patch.json"
    patch.write_text(json.dumps([{"op":"test","path":"/metadata/uid","value":"owned-uid"},{"op":"replace","path":"/data/tls.key","value":base64.b64encode(b"new-synthetic-key").decode()}]))
    patch.chmod(0o600)
    result=subprocess.run([str(binary),"patch","--local","-f",str(base),"--type=json","--patch-file",str(patch),"-o","json"],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)["data"]["tls.key"]==base64.b64encode(b"new-synthetic-key").decode()


@pytest.mark.parametrize("major", [3, 4])
def test_operator_bootstrap_lists_all_statuses_with_both_helm_majors(tmp_path, major):
    """Execute setup through its real shared compatibility helper, without installs."""
    import shutil, subprocess
    root = Path(__file__).parents[1]
    environment, log, created = setup_script(tmp_path, crd=False)
    project = tmp_path / "source"
    (project / "scripts").mkdir(parents=True)
    for name in ("setup-cert-manager.sh", "cert-manager.lock.json"):
        shutil.copyfile(root / "scripts" / name, project / "scripts" / name)
    # Preserve the real compatibility helper while isolating the optional
    # bootstrap's tool cache from the developer's actual home directory.
    (project / "scripts/installer.py").write_text(
        "import importlib.util,pathlib\n"
        f"spec=importlib.util.spec_from_file_location('real_installer',{str(root / 'scripts/installer.py')!r})\n"
        "core=importlib.util.module_from_spec(spec);spec.loader.exec_module(core)\n"
        "InstallError=core.InstallError\n"
        "resolve_kubernetes_target=core.resolve_kubernetes_target\n"
        "helm_list_status_flags=core.helm_list_status_flags\n"
        f"def secure_directory(*args):\n p=pathlib.Path({str(tmp_path / 'private-tools')!r});p.mkdir(exist_ok=True,mode=0o700);return p\n"
    )
    helm = tmp_path / "bin/helm"
    helm.write_text('''#!/usr/bin/env python3
import json,os,sys
args=sys.argv[1:]
with open(os.environ['WF_SETUP_TEST_LOG'],'a') as output:output.write(json.dumps(['helm',*args])+'\\n')
major=int(os.environ['WF_SETUP_TEST_HELM_MAJOR'])
if args[:2]==['version','--short']:print('v'+str(major)+'.2.3+gtest')
elif 'list' in args:
    if ('--all' in args)!=(major==3):raise SystemExit('incompatible status flag')
    print(json.dumps([{'name':'pre-existing-operator'}]))
else:raise SystemExit('No operator install is permitted in this test')
''')
    helm.chmod(0o700)
    environment["WF_SETUP_TEST_HELM_MAJOR"] = str(major)
    result = subprocess.run(["bash", str(project / "scripts/setup-cert-manager.sh"), "--context", "explicit-test",
                             "--namespace", "wealthfolio", "--issuer", "chosen-issuer",
                             "--email", "operator@example.test", "--install-controller"],
                            env=environment, capture_output=True, text=True)
    assert result.returncode != 0 and "existing cert-manager namespace release" in result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    listing = next(command for command in commands if command[:2] == ["helm", "list"])
    assert ("--all" in listing) is (major == 3)
    assert not any(command[0] == "helm" and "install" in command for command in commands)
    assert not created.exists()
