"""Installer state transitions and credential boundaries without a real cluster."""
import base64
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("wealthfolio_installer", Path(__file__).parents[1] / "scripts/installer.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class Cluster:
    def __init__(self):
        self.namespace = None
        self.secret = None
        self.claim = None
        self.release = None
        self.calls = []
        self.secret_creations = 0
        self.installations = 0
        self.secret_response_lost = False
        self.install_response_lost = False
        self.failed_install = False
        self.endpoint = "https://test.invalid"
        self.tls_secret = None
        self.deployment = None
        self.values = None
        self.upgrades = 0
        self.failed_upgrade = False
        self.upgrade_response_lost = False
        self.fail_before_upgrade = False
        self.helm_version = "v3.22.1+fixture"
        self.current_context = "safe-test"

    def __call__(self, command, *, data=None, check=True):
        command = [str(x) for x in command]
        self.calls.append((command, data))
        output = ""
        rc = 0
        if command[1:] == ["version", "--short"]:
            output = self.helm_version
        elif "version" in command:
            output = json.dumps({"serverVersion": {"gitVersion": "v1.35.8"}})
        elif "config" in command:
            output = self.current_context if "current-context" in command else self.endpoint
        elif "get" in command and "values" in command:
            output = json.dumps(self.values)
        elif "get" in command:
            kind = command[command.index("get") + 1]
            value = {"namespace": self.namespace, "secret": self.secret, "pvc": self.claim, "deployment": self.deployment}[kind]
            if kind == "secret" and command[command.index("get") + 2] == "existing-tls":
                value = self.tls_secret
            if value:
                output = json.dumps(value["metadata"] if kind == "secret" else value)
                if kind == "secret":
                    assert command[-1] == "jsonpath={.metadata}", "Existing Secret contents must never be requested as output"
        elif "list" in command:
            if self.helm_version.startswith("v4.") and "--all" in command:
                raise installer.InstallError("Unsupported Helm 4 --all flag")
            output = json.dumps([self.release] if self.release else [])
        elif "template" in command:
            values = Path(command[command.index("--values") + 1]).read_text()
            assert "WF_SECRET_KEY" not in values and "$argon2" not in values
        elif "create" in command:
            payload = json.loads(data)
            if payload["kind"] == "Namespace":
                self.namespace = {"metadata": {"uid": "namespace-uid"}}
            elif payload["kind"] == "Secret":
                self.secret_creations += 1
                assert len(base64.b64decode(payload["stringData"]["WF_SECRET_KEY"])) == 32
                self.secret = {"metadata": {"uid": "secret-uid", "labels": payload["metadata"]["labels"]}}
                if self.secret_response_lost:
                    self.secret_response_lost = False
                    rc = 1
        elif "install" in command:
            self.installations += 1
            self.values = json.loads(Path(command[command.index("--values") + 1]).read_text())
            self.release = {"name": "wealthfolio", "status": "failed" if self.failed_install else "deployed", "chart": "wealthfolio-0.1.0", "app_version": "3.11.0", "revision": "1"}
            self.claim = {"metadata": {"uid": "claim-uid", "annotations": {"meta.helm.sh/release-name": "wealthfolio", "meta.helm.sh/release-namespace": "wealthfolio"}}, "status": {"phase": "Bound"}}
            if self.install_response_lost or self.failed_install:
                self.install_response_lost = False
                rc = 1
        elif "upgrade" in command:
            self.upgrades += 1
            assert "--reuse-values" in command
            override = json.loads(Path(command[command.index("--values") + 1]).read_text())
            assert set(override) == {"auth", "app", "ingress"}
            assert set(override["auth"]) == {"cookieSecure"}
            assert set(override["app"]) == {"corsAllowOrigins"}
            if not self.fail_before_upgrade:
                for key, changes in override.items():
                    self.values[key] = {**self.values.get(key, {}), **changes}
            self.release["revision"] = str(int(self.release["revision"]) + 1)
            self.release["status"] = "failed" if self.failed_upgrade or self.fail_before_upgrade else "deployed"
            if self.failed_upgrade or self.upgrade_response_lost or self.fail_before_upgrade:
                self.upgrade_response_lost = False
                rc = 1
        if rc and check:
            raise installer.InstallError("Synthetic transport failure; output withheld")
        return subprocess.CompletedProcess(command, rc, output, "")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name).resolve() / "recovery"
        self.config = Path(self.temporary.name).resolve() / "client.yaml"
        self.config.write_text("synthetic client configuration")
        self.cluster = Cluster()
        self.output = io.StringIO()
        self.password = "long synthetic password"
        self.hash = "$argon2id$v=19$m=65536,t=3,p=1$synthetic$synthetic"

    def tearDown(self):
        self.temporary.cleanup()

    def install(self, *extra, use_saved=False, answers=None, interactive=True, use_current=False):
        mode = [] if "--hostname" in extra or use_saved or "--configure-access" in extra else ["--local-access"]
        target = [] if use_current else ["--context", "safe-test"]
        args = installer.parser().parse_args([*target, "--kubeconfig", str(self.config), "--state-dir", str(self.state), "--no-access", *mode, *extra])
        with mock.patch.object(installer, "run", self.cluster), mock.patch.object(installer.shutil, "which", return_value="/usr/bin/true"), mock.patch.object(installer.sys.stdin, "isatty", return_value=interactive), mock.patch.object(installer.getpass, "getpass", return_value=self.password), mock.patch.object(installer, "hash_password", return_value=self.hash), mock.patch("builtins.input", side_effect=["http"] if answers is None else answers) as prompt, contextlib.redirect_stdout(self.output):
            try:
                installer.Installer(args).execute()
            finally:
                self.prompts = [call.args[0] for call in prompt.call_args_list]

    def test_fresh_install_keeps_credentials_out_of_arguments_values_and_output(self):
        self.install()
        record = json.loads((self.state / "state.json").read_text())
        self.assertEqual(record["status"], "installed")
        key = (self.state / "master-key").read_text().strip()
        self.assertEqual(len(base64.b64decode(key)), 32)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for filename in ("state.json", "master-key", "password-hash", "values.json"):
            self.assertEqual(stat.S_IMODE((self.state / filename).stat().st_mode), 0o600)
        for command, _ in self.cluster.calls:
            arguments = " ".join(command)
            self.assertNotIn(self.password, arguments)
            self.assertNotIn(self.hash, arguments)
            self.assertNotIn(key, arguments)
            if "--context" in command:
                self.assertEqual(command[command.index("--context") + 1], "safe-test")
        text = self.output.getvalue() + (self.state / "values.json").read_text()
        for credential in (self.password, self.hash, key):
            self.assertNotIn(credential, text)
        values = json.loads((self.state / "values.json").read_text())
        self.assertEqual(values["image"]["repository"], installer.OFFICIAL_REPOSITORY)
        self.assertEqual(values["image"]["digest"], installer.OFFICIAL_DIGEST)
        rendering = next(command for command, _ in self.cluster.calls if "template" in command)
        self.assertEqual(rendering[rendering.index("--kube-version") + 1], "v1.35.8")

    def test_retry_does_not_rotate_secret_or_run_helm_upgrade(self):
        self.install()
        key = (self.state / "master-key").read_bytes()
        self.install()
        self.assertEqual(self.cluster.secret_creations, 1)
        self.assertEqual(self.cluster.installations, 1)
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        self.assertFalse(any("upgrade" in command for command, _ in self.cluster.calls))

    def test_helm4_configure_reaches_questions_and_preserves_existing_credentials(self):
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        key = (self.state / "master-key").read_bytes()
        secret_uid = self.cluster.secret["metadata"]["uid"]
        claim_uid = self.cluster.claim["metadata"]["uid"]
        self.cluster.helm_version = "v4.2.3+g43e8b7f"
        self.install("--configure-access", answers=["https", "controller", "yes"])
        self.assertTrue(any("Browser protocol" in prompt for prompt in self.prompts))
        self.assertEqual(self.cluster.upgrades, 1)
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        self.assertEqual(self.cluster.secret["metadata"]["uid"], secret_uid)
        self.assertEqual(self.cluster.claim["metadata"]["uid"], claim_uid)
        lists = [command for command, _ in self.cluster.calls if "list" in command]
        self.assertNotIn("--all", lists[-1])
        # Helm 3 still includes non-default statuses rather than losing the
        # existing-release/history guard while making Helm 4 compatible.
        self.cluster.helm_version = "v3.22.1+fixture"
        self.install("--hostname", "wealthfolio.example.lan", answers=[])
        self.assertIn("--all", [command for command, _ in self.cluster.calls if "list" in command][-1])

    def test_helm4_failed_release_is_not_reinstalled_or_adopted(self):
        self.install()
        original_state = (self.state / "state.json").read_bytes()
        key = (self.state / "master-key").read_bytes()
        self.cluster.helm_version = "v4.2.3+g43e8b7f"
        self.cluster.release["status"] = "failed"
        with self.assertRaisesRegex(installer.InstallError, "previous Helm install is incomplete"):
            self.install("--configure-access", answers=[])
        self.assertEqual((self.state / "state.json").read_bytes(), original_state)
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        self.assertEqual(self.cluster.installations, 1)
        self.assertEqual(self.cluster.secret_creations, 1)
        self.assertEqual(self.cluster.upgrades, 0)
        self.assertFalse(any("upgrade" in command for command, _ in self.cluster.calls))

    def test_lost_secret_response_recovers_only_matching_installation_identity(self):
        self.cluster.secret_response_lost = True
        with self.assertRaises(installer.InstallError):
            self.install()
        key = (self.state / "master-key").read_bytes()
        self.install()
        self.assertEqual(self.cluster.secret_creations, 1)
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        self.assertEqual(json.loads((self.state / "state.json").read_text())["status"], "installed")

    def test_lost_successful_helm_response_recovers_without_upgrade(self):
        self.cluster.install_response_lost = True
        with self.assertRaises(installer.InstallError):
            self.install()
        self.install()
        self.assertEqual(self.cluster.installations, 1)
        self.assertEqual(json.loads((self.state / "state.json").read_text())["claimUid"], "claim-uid")

    def test_failed_release_never_upgrades_or_deletes_storage(self):
        self.cluster.failed_install = True
        with self.assertRaises(installer.InstallError):
            self.install()
        key = (self.state / "master-key").read_bytes()
        with self.assertRaisesRegex(installer.InstallError, "incomplete"):
            self.install()
        self.assertIn("Recovery directory: " + str(self.state), self.output.getvalue())
        self.assertNotIn(key.decode().strip(), self.output.getvalue())
        self.assertNotIn(self.hash, self.output.getvalue())
        self.assertEqual(self.cluster.installations, 1)
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        self.assertFalse(any("delete" in command for command, _ in self.cluster.calls))

    def test_replaced_secret_or_namespace_refused(self):
        self.install()
        self.cluster.secret["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(installer.InstallError, "replaced"):
            self.install()
        self.cluster.secret["metadata"]["uid"] = "secret-uid"
        self.cluster.namespace["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(installer.InstallError, "namespace was replaced"):
            self.install()

    def test_retained_claim_is_not_implicitly_adopted(self):
        self.cluster.namespace = {"metadata": {"uid": "namespace-uid"}}
        self.cluster.claim = {"metadata": {"uid": "valuable-claim"}}
        with self.assertRaisesRegex(installer.InstallError, "retained"):
            self.install()
        self.assertEqual(self.cluster.secret_creations, 0)
        self.assertEqual(self.cluster.installations, 0)

    def test_existing_unrelated_secret_requires_explicit_reference(self):
        self.cluster.namespace = {"metadata": {"uid": "namespace-uid"}}
        self.cluster.secret = {"metadata": {"uid": "existing-secret"}}
        with self.assertRaisesRegex(installer.InstallError, "explicitly"):
            self.install()
        self.assertEqual(self.cluster.secret_creations, 0)
        self.install("--existing-secret", "wealthfolio-auth")
        self.assertIn("original master key independently", self.output.getvalue())
        self.assertNotIn("Keep its master-key file", self.output.getvalue())
        self.assertFalse((self.state / "master-key").exists())
        self.assertEqual(self.cluster.secret_creations, 0)

    def test_changed_target_cannot_reuse_recovery_state(self):
        self.install()
        self.cluster.endpoint = "https://other.invalid"
        with self.assertRaisesRegex(installer.InstallError, "different target"):
            self.install()

    def test_deleted_namespace_cannot_be_resurrected_with_old_state(self):
        self.install()
        self.cluster.namespace = None
        with self.assertRaisesRegex(installer.InstallError, "recorded namespace is missing"):
            self.install()

    def test_installed_state_does_not_hide_failed_release(self):
        self.install()
        self.cluster.release["status"] = "failed"
        with self.assertRaisesRegex(installer.InstallError, "incomplete"):
            self.install()
        self.assertEqual(self.cluster.installations, 1)

    def test_current_context_install_and_retry_keep_credentials_and_pin_every_target(self):
        secure_directory = installer.secure_directory
        def check_target_before_state(*args, **kwargs):
            self.assertIn("context safe-test", self.output.getvalue())
            self.assertIn("API https://test.invalid", self.output.getvalue())
            return secure_directory(*args, **kwargs)
        with mock.patch.object(installer, "secure_directory", side_effect=check_target_before_state):
            self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", use_current=True, answers=[])
        original_key = (self.state / "master-key").read_bytes()
        self.install("--hostname", "wealthfolio.example.lan", use_current=True, answers=[])
        self.assertEqual(self.cluster.installations, 1)
        self.assertEqual(self.cluster.secret_creations, 1)
        self.assertEqual((self.state / "master-key").read_bytes(), original_key)
        self.assertEqual(self.cluster.values["ingress"]["className"], "")
        self.assertIn("context safe-test", self.output.getvalue())
        self.assertIn("API https://test.invalid", self.output.getvalue())
        for command, _ in self.cluster.calls:
            if "current-context" in command or "template" in command or command[1:] == ["version", "--short"]:
                continue
            self.assertTrue("--context" in command or "--kube-context" in command, command)
            for flag in ("--context", "--kube-context"):
                if flag in command:
                    self.assertEqual(command[command.index(flag) + 1], "safe-test")
        self.cluster.current_context = "different-target"
        with self.assertRaisesRegex(installer.InstallError, "different target"):
            self.install("--hostname", "wealthfolio.example.lan", use_current=True, answers=[])
        self.assertEqual(self.cluster.installations, 1)

    def test_current_context_unattended_install_with_existing_secret_has_no_extra_gate(self):
        self.cluster.namespace = {"metadata": {"uid": "namespace-uid"}}
        self.cluster.secret = {"metadata": {"uid": "existing-secret-uid"}}
        self.install("--existing-secret", "wealthfolio-auth", use_current=True, interactive=False, answers=[])
        self.assertEqual(self.cluster.installations, 1)
        self.assertEqual(self.prompts, [])

    def test_target_display_does_not_expose_url_credentials_or_query(self):
        self.cluster.endpoint = "https://operator:private-api-pass@cluster.example.test:6443/api?token=private-query-value#hidden"
        self.install()
        output = self.output.getvalue()
        self.assertIn("API https://cluster.example.test:6443/api", output)
        self.assertNotIn("private-api-pass", output)
        self.assertNotIn("private-query-value", output)

    def test_locked_helm_bootstrap_covers_supported_hosts_and_rejects_corruption(self):
        locks = json.loads((installer.ROOT / "tools.lock.json").read_text())
        source = Path(self.temporary.name) / "source"
        source.mkdir()
        for system, machine, host in (("Linux", "aarch64", "linux-arm64"),
                                      ("Linux", "x86_64", "linux-amd64"),
                                      ("Darwin", "arm64", "darwin-arm64"),
                                      ("Darwin", "x86_64", "darwin-amd64")):
            with self.subTest(host=host):
                spec = dict(locks["platforms"][host]["helm"])
                self.assertEqual(spec["url"], f"https://get.helm.sh/helm-v{spec['version']}-{host}.tar.gz")
                self.assertEqual(spec["member"], host + "/helm")
                executable = b"synthetic Helm executable"
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
                    member = tarfile.TarInfo(spec["member"])
                    member.size = len(executable)
                    bundle.addfile(member, io.BytesIO(executable))
                payload = archive.getvalue()
                spec["sha256"] = hashlib.sha256(payload).hexdigest()
                (source / "tools.lock.json").write_text(json.dumps({"platforms": {host: {"helm": spec}}}))
                output = Path(self.temporary.name) / host
                with mock.patch.object(installer, "ROOT", source), mock.patch.object(installer.platform, "system", return_value=system), mock.patch.object(installer.platform, "machine", return_value=machine), mock.patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(b"corrupted")):
                    with self.assertRaisesRegex(installer.InstallError, "checksum"):
                        installer.locked_tool("helm", output)
                    self.assertFalse((output / "helm").exists())
                with mock.patch.object(installer, "ROOT", source), mock.patch.object(installer.platform, "system", return_value=system), mock.patch.object(installer.platform, "machine", return_value=machine), mock.patch.object(installer.urllib.request, "urlopen", return_value=io.BytesIO(payload)) as download:
                    result = installer.locked_tool("helm", output)
                    self.assertEqual(Path(result).read_bytes(), executable)
                    self.assertEqual(stat.S_IMODE(Path(result).stat().st_mode), 0o700)
                    self.assertEqual(installer.locked_tool("helm", output), result)
                    download.assert_called_once_with(spec["url"], timeout=120)

    def test_access_only_never_installs(self):
        with self.assertRaisesRegex(installer.InstallError, "No completed"):
            self.install("--access-only")
        self.assertEqual(self.cluster.installations, 0)
        self.assertEqual(self.cluster.secret_creations, 0)

    def test_recovery_symlink_and_world_readable_state_refused(self):
        self.install()
        state = self.state / "state.json"
        state.chmod(0o644)
        with self.assertRaisesRegex(installer.InstallError, "0600"):
            self.install()
        state.chmod(0o600)
        original = self.state / "old.json"
        state.rename(original)
        state.symlink_to(original)
        with self.assertRaisesRegex(installer.InstallError, "regular"):
            self.install()

    def test_ubuntu_venv_helper_installs_only_targeted_dependency(self):
        binaries = Path(self.temporary.name) / "bin"
        binaries.mkdir()
        log = Path(self.temporary.name) / "apt.log"
        ready = Path(self.temporary.name) / "ready"
        scripts = {
            "id": "#!/bin/sh\nprintf '0\\n'\n",
            "python3": '#!/bin/sh\ntest -f "$READY"\n',
            "apt-get": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$APT_LOG"\nif test "$1" = install; then touch "$READY"; fi\n',
        }
        for name, content in scripts.items():
            path = binaries / name
            path.write_text(content)
            path.chmod(0o700)
        environment = dict(os.environ, PATH=str(binaries) + ":/usr/bin:/bin", APT_LOG=str(log), READY=str(ready))
        result = subprocess.run(["bash", str(installer.ROOT / "scripts/prepare-python.sh")], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().splitlines(), ["update", "install --yes --no-install-recommends python3-venv"])
        self.assertIn("no system upgrade", result.stdout)

    def test_http_ingress_configures_external_origin_and_stable_url_without_forwarding(self):
        self.install("--hostname", "wealthfolio.example.lan", "--ingress-class", "traefik")
        values = json.loads((self.state / "values.json").read_text())
        self.assertEqual(values["ingress"], {"enabled": True, "className": "traefik", "hosts": ["wealthfolio.example.lan"], "tls": []})
        self.assertEqual(values["app"]["corsAllowOrigins"], ["http://wealthfolio.example.lan"])
        self.assertEqual(values["auth"]["cookieSecure"], "false")
        self.assertIn("Open http://wealthfolio.example.lan", self.output.getvalue())
        self.assertNotIn("port-forward", self.output.getvalue())
        self.assertNotIn("http://localhost", self.output.getvalue())

    def test_https_ingress_uses_existing_tls_secret_secure_cookie_and_exact_origin(self):
        self.cluster.namespace = {"metadata": {"uid": "namespace-uid"}}
        self.cluster.tls_secret = {"metadata": {"uid": "tls-uid"}}
        self.install("--hostname", "wealthfolio.example.lan", "--tls-secret", "existing-tls")
        values = json.loads((self.state / "values.json").read_text())
        self.assertEqual(values["ingress"]["tls"], [{"secretName": "existing-tls", "hosts": ["wealthfolio.example.lan"]}])
        self.assertEqual(values["app"]["corsAllowOrigins"], ["https://wealthfolio.example.lan"])
        self.assertEqual(values["auth"]["cookieSecure"], "true")
        self.assertIn("Open https://wealthfolio.example.lan", self.output.getvalue())
        self.assertEqual(self.cluster.tls_secret["metadata"]["uid"], "tls-uid")
        self.assertEqual(self.cluster.secret_creations, 1)  # Auth only; TLS never created.
        self.assertEqual(self.prompts, [])

    def test_missing_tls_secret_fails_before_creating_namespace_or_credentials(self):
        with self.assertRaisesRegex(installer.InstallError, "TLS Secret must already exist"):
            self.install("--hostname", "wealthfolio.example.lan", "--tls-secret", "existing-tls")
        self.assertEqual(self.cluster.secret_creations, 0)
        self.assertIsNone(self.cluster.namespace)

    def test_ingress_class_is_cluster_default_or_explicit_without_installing_a_controller(self):
        args = installer.parser().parse_args(["--context", "test", "--kubeconfig", str(self.config), "--hostname", "wealthfolio.example.lan"])
        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/true"):
            instance = installer.Installer(args)
        self.assertEqual(instance.network_values()["ingress"]["className"], "")
        args.ingress_class = "nginx"
        self.assertEqual(instance.network_values()["ingress"]["className"], "nginx")

    def test_explicit_kubeconfig_and_context_do_not_require_root_or_read_credentials(self):
        config = Path(self.temporary.name) / "custom cluster.yaml"
        config.write_text("private kubeconfig never copied")
        args = installer.parser().parse_args(["--kubeconfig", str(config),
                                              "--context", "edge-target", "--local-access"])
        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/true"), mock.patch.object(installer, "run") as command:
            instance = installer.Installer(args)
            self.assertEqual(instance.kubectl, ["/usr/bin/true", "--kubeconfig", str(config.absolute()), "--context", "edge-target"])
            self.assertEqual(instance.helm_target, ["--kubeconfig", str(config.absolute()), "--kube-context", "edge-target"])
            self.assertEqual(instance.context, "edge-target")
            command.assert_not_called()

    def test_custom_ingress_port_preserves_tls_host_and_credentials_and_changes_only_explicitly(self):
        host = "wealthfolio.example.lan"
        self.install("--hostname", host, "--scheme", "https", "--tls-mode", "controller", "--ingress-port", "8443", answers=[])
        original_key = (self.state / "master-key").read_bytes()
        original_claim = self.cluster.claim["metadata"]["uid"]
        self.assertEqual(self.cluster.values["app"]["corsAllowOrigins"], ["https://" + host + ":8443"])
        self.assertEqual(self.cluster.values["ingress"]["tls"], [{"hosts": [host]}])
        self.assertEqual(self.cluster.values["auth"]["cookieSecure"], "true")
        self.assertIn("Open https://" + host + ":8443", self.output.getvalue())
        self.install("--hostname", host, answers=[])
        with self.assertRaisesRegex(installer.InstallError, "saved Ingress port differs"):
            self.install("--hostname", host, "--ingress-port", "9443", answers=[])
        self.assertEqual(self.cluster.upgrades, 0)
        self.install("--configure-access", "--scheme", "https", "--tls-mode", "controller", "--ingress-port", "9443", answers=["yes"])
        self.assertEqual(self.cluster.values["app"]["corsAllowOrigins"], ["https://" + host + ":9443"])
        self.assertEqual(self.cluster.upgrades, 1)
        self.assertEqual(self.cluster.secret_creations, 1)
        self.assertEqual(self.cluster.claim["metadata"]["uid"], original_claim)
        self.assertEqual((self.state / "master-key").read_bytes(), original_key)

    def test_ipv6_listener_is_initial_only_and_retained_by_access_changes(self):
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", "--listen-address", "::", answers=[])
        self.assertEqual(self.cluster.values["app"]["listenAddress"], "::")
        self.install("--configure-access", "--scheme", "https", "--tls-mode", "controller", answers=["yes"])
        self.assertEqual(self.cluster.values["app"]["listenAddress"], "::")
        with self.assertRaisesRegex(installer.InstallError, "listener differs"):
            self.install("--hostname", "wealthfolio.example.lan", "--listen-address", "0.0.0.0", answers=[])

    def test_unsupported_access_combinations_fail_before_target_queries(self):
        combinations = [
            [],
            ["--hostname", "192.0.2.5"],
            ["--hostname", "https://wealthfolio.example.lan"],
            ["--hostname", "*.example.lan"],
            ["--hostname", "wealthfolio.example.lan", "--local-access"],
            ["--hostname", "wealthfolio.example.lan", "--scheme", "http", "--tls-secret", "existing-tls"],
            ["--local-access", "--scheme", "http"],
            ["--local-access", "--ingress-class", "traefik"],
            ["--local-access", "--ingress-port", "8443"],
            ["--hostname", "wealthfolio.example.lan", "--ingress-port", "0"],
            ["--hostname", "wealthfolio.example.lan", "--ingress-port", "65536"],
        ]
        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/true"), mock.patch.object(installer, "run") as command:
            for flags in combinations:
                with self.subTest(flags=flags), self.assertRaises(installer.InstallError):
                    installer.Installer(installer.parser().parse_args(["--context", "test", "--kubeconfig", str(self.config), *flags]))
            command.assert_not_called()

    def test_ingress_retry_preserves_url_and_rejects_implicit_network_changes(self):
        flags = ["--hostname", "wealthfolio.example.lan", "--ingress-class", "traefik"]
        self.install(*flags)
        key = (self.state / "master-key").read_bytes()
        self.install(*flags)
        for changed in (["--hostname", "other.example.lan"], ["--hostname", "wealthfolio.example.lan", "--ingress-class", "other"]):
            with self.assertRaisesRegex(installer.InstallError, "explicit Helm upgrade"):
                self.install(*changed)
        with self.assertRaisesRegex(installer.InstallError, "uses Ingress at http://wealthfolio.example.lan"):
            self.install("--access-only")
        self.assertEqual(self.cluster.installations, 1)
        self.assertEqual(self.cluster.secret_creations, 1)
        self.assertEqual((self.state / "master-key").read_bytes(), key)

    def test_local_install_cannot_be_changed_to_ingress_on_retry(self):
        self.install()
        with self.assertRaisesRegex(installer.InstallError, "explicit Helm upgrade"):
            self.install("--hostname", "wealthfolio.example.lan")
        self.assertEqual(self.cluster.installations, 1)

    def test_https_retry_reuses_saved_url_and_rejects_tls_or_scheme_changes(self):
        self.cluster.namespace = {"metadata": {"uid": "namespace-uid"}}
        self.cluster.tls_secret = {"metadata": {"uid": "tls-uid"}}
        self.install("--hostname", "wealthfolio.example.lan", "--tls-secret", "existing-tls")
        self.install("--access-only", use_saved=True)
        for changed in (["--scheme", "http"], ["--tls-secret", "other-tls"]):
            with self.assertRaisesRegex(installer.InstallError, "explicit Helm upgrade"):
                self.install("--hostname", "wealthfolio.example.lan", *changed)
        self.assertIn("Open https://wealthfolio.example.lan", self.output.getvalue())
        self.assertNotIn("port-forward", self.output.getvalue())
        self.assertEqual(self.cluster.installations, 1)
        self.assertEqual(self.cluster.secret_creations, 1)

    def prepare_tls(self):
        self.cluster.namespace = {"metadata": {"uid": "namespace-uid"}}
        self.cluster.tls_secret = {"metadata": {"uid": "tls-uid"}}

    def test_fresh_missing_protocol_offers_https_and_tls_selection_even_with_yes(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", "--yes", answers=["https", "secret", "existing-tls"])
        self.assertEqual(len(self.prompts), 3)
        self.assertIn("Browser protocol", self.prompts[0])
        self.assertIn("Certificate method", self.prompts[1])
        self.assertIn("TLS Secret", self.prompts[2])
        self.assertEqual(self.cluster.values["auth"]["cookieSecure"], "true")
        self.assertEqual(self.cluster.values["app"]["corsAllowOrigins"], ["https://wealthfolio.example.lan"])

    def test_explicit_scheme_or_tls_skips_redundant_protocol_question(self):
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        self.assertEqual(self.prompts, [])

    def test_explicit_https_prompts_for_only_missing_tls_name(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "https", "--tls-mode", "secret", answers=["existing-tls"])
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("TLS Secret", self.prompts[0])

    def test_noninteractive_missing_protocol_never_defaults_to_http(self):
        with self.assertRaisesRegex(installer.InstallError, "HTTP is never selected silently"):
            self.install("--hostname", "wealthfolio.example.lan", interactive=False, answers=[])
        self.assertIsNone(self.cluster.namespace)
        self.assertEqual(self.cluster.secret_creations, 0)
        self.assertEqual(self.cluster.installations, 0)

    def test_noninteractive_explicit_https_requires_tls_before_writes(self):
        with self.assertRaisesRegex(installer.InstallError, "Choose --tls-mode"):
            self.install("--hostname", "wealthfolio.example.lan", "--scheme", "https", interactive=False, answers=[])
        self.assertEqual(self.cluster.secret_creations, 0)
        self.assertIsNone(self.cluster.namespace)

    def test_partial_https_retry_reuses_choice_without_downgrading_or_reprompting(self):
        self.prepare_tls()
        self.cluster.secret_response_lost = True
        with self.assertRaises(installer.InstallError):
            self.install("--hostname", "wealthfolio.example.lan", answers=["https", "secret", "existing-tls"])
        key = (self.state / "master-key").read_bytes()
        self.install("--hostname", "wealthfolio.example.lan", answers=[])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.cluster.values["auth"]["cookieSecure"], "true")
        self.assertEqual((self.state / "master-key").read_bytes(), key)

    def test_existing_rerun_reuses_saved_scheme_without_prompt(self):
        self.install("--hostname", "wealthfolio.example.lan", answers=["http"])
        self.install("--hostname", "wealthfolio.example.lan", answers=[])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.cluster.upgrades, 0)

    def test_configure_access_changes_only_network_and_preserves_identities(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", answers=["http"])
        before = copy.deepcopy(self.cluster.values)
        key = (self.state / "master-key").read_bytes()
        self.install("--configure-access", answers=["https", "secret", "existing-tls", "yes"])
        record = json.loads((self.state / "state.json").read_text())
        self.assertEqual(self.cluster.upgrades, 1)
        self.assertEqual(self.cluster.release["revision"], "2")
        self.assertEqual(record["secretUid"], "secret-uid")
        self.assertEqual(record["claimUid"], "claim-uid")
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        for field in ("image", "encryption", "fullnameOverride"):
            self.assertEqual(self.cluster.values[field], before[field])
        self.assertEqual(self.cluster.values["auth"]["existingSecret"], before["auth"]["existingSecret"])
        self.assertEqual(self.cluster.values["ingress"]["className"], before["ingress"]["className"])
        self.assertEqual(self.cluster.values["auth"]["cookieSecure"], "true")
        self.assertEqual(record["values"], self.cluster.values)
        self.assertNotIn("pendingAccess", record)

    def test_configure_enter_keeps_current_scheme_without_upgrade(self):
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        self.install("--configure-access", answers=[""])
        self.assertEqual(self.cluster.upgrades, 0)
        self.assertEqual(self.cluster.release["revision"], "1")

    def test_explicit_configure_flags_work_without_interactive_questions(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        self.install("--configure-access", "--scheme", "https", "--tls-secret", "existing-tls", interactive=False, answers=[])
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.cluster.values["auth"]["cookieSecure"], "true")
        self.assertEqual(self.cluster.upgrades, 1)

    def test_configure_decline_or_missing_tls_does_not_change_saved_config(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        before = (self.state / "state.json").read_bytes()
        self.install("--configure-access", answers=["https", "secret", "existing-tls", "no"])
        self.assertEqual((self.state / "state.json").read_bytes(), before)
        self.cluster.tls_secret = None
        with self.assertRaisesRegex(installer.InstallError, "TLS Secret must already exist"):
            self.install("--configure-access", answers=["https", "secret", "existing-tls"])
        self.assertEqual((self.state / "state.json").read_bytes(), before)
        self.assertEqual(self.cluster.upgrades, 0)

    def test_configure_rejects_manual_values_or_chart_drift(self):
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        self.cluster.values["encryption"]["required"] = True
        with self.assertRaisesRegex(installer.InstallError, "values changed outside"):
            self.install("--configure-access", answers=[])
        self.cluster.values["encryption"]["required"] = False
        self.cluster.release["chart"] = "wealthfolio-0.2.0"
        with self.assertRaisesRegex(installer.InstallError, "same installed chart"):
            self.install("--configure-access", answers=[])
        self.assertEqual(self.cluster.upgrades, 0)

    def test_failed_configure_retains_original_config_and_explicit_retry_completes(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        original = copy.deepcopy(self.cluster.values)
        key = (self.state / "master-key").read_bytes()
        self.cluster.failed_upgrade = True
        with self.assertRaisesRegex(installer.InstallError, "access upgrade did not complete"):
            self.install("--configure-access", answers=["https", "secret", "existing-tls", "yes"])
        pending = json.loads((self.state / "state.json").read_text())
        self.assertEqual(pending["values"], original)
        self.assertEqual(pending["pendingAccess"]["status"], "failed")
        with self.assertRaisesRegex(installer.InstallError, "scheme differs"):
            self.install("--configure-access", "--scheme", "http", answers=[])
        self.cluster.failed_upgrade = False
        self.install("--configure-access", answers=[])
        self.assertNotIn("pendingAccess", json.loads((self.state / "state.json").read_text()))
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        self.assertEqual(self.cluster.upgrades, 2)

    def test_lost_successful_upgrade_response_verifies_without_duplicate_upgrade(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        self.cluster.upgrade_response_lost = True
        with self.assertRaises(installer.InstallError):
            self.install("--configure-access", answers=["https", "secret", "existing-tls", "yes"])
        with self.assertRaisesRegex(installer.InstallError, "pending verification"):
            self.install("--hostname", "wealthfolio.example.lan", answers=[])
        self.install("--configure-access", answers=[])
        self.assertEqual(self.cluster.upgrades, 1)
        self.assertEqual(self.cluster.release["revision"], "2")

    def test_failed_before_apply_can_retry_only_recorded_target(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        original = copy.deepcopy(self.cluster.values)
        self.cluster.fail_before_upgrade = True
        with self.assertRaisesRegex(installer.InstallError, "access upgrade did not complete"):
            self.install("--configure-access", answers=["https", "secret", "existing-tls", "yes"])
        self.assertEqual(self.cluster.values, original)
        with self.assertRaisesRegex(installer.InstallError, "retry explicitly|Retry explicitly"):
            self.install("--hostname", "wealthfolio.example.lan", answers=[])
        self.cluster.fail_before_upgrade = False
        self.install("--configure-access", answers=[])
        self.assertEqual(self.cluster.values["auth"]["cookieSecure"], "true")
        self.assertEqual(self.cluster.upgrades, 2)

    def test_configure_accepts_legacy_claim_only_with_helm_ownership(self):
        self.prepare_tls()
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        path = self.state / "state.json"
        state = json.loads(path.read_text())
        state.pop("claimUid")
        path.write_text(json.dumps(state))
        self.install("--configure-access", answers=["https", "secret", "existing-tls", "yes"])
        result = json.loads(path.read_text())
        self.assertEqual(result["claimUid"], "claim-uid")
        self.assertEqual(result["lastAccessChange"]["claimIdentitySource"], "current-helm-owned-legacy-snapshot")

    def test_controller_https_needs_no_certificate_secret_or_operator(self):
        self.install("--hostname", "wealthfolio.example.lan", answers=["https", "controller"])
        values = self.cluster.values
        self.assertEqual(values["ingress"]["tls"], [{"hosts": ["wealthfolio.example.lan"]}])
        self.assertEqual(values["app"]["corsAllowOrigins"], ["https://wealthfolio.example.lan"])
        self.assertEqual(values["auth"]["cookieSecure"], "true")
        self.assertEqual(self.cluster.secret_creations, 1)  # Authentication only.
        self.assertEqual(json.loads((self.state / "state.json").read_text())["tlsOptions"]["mode"], "controller")
        self.assertIn("untrusted generated certificate", self.output.getvalue())
        self.assertFalse(any("certificate" in command or "issuer" in command for command, _ in self.cluster.calls))

    def test_controller_partial_retry_and_noop_preserve_credential_and_revision(self):
        self.cluster.secret_response_lost = True
        with self.assertRaises(installer.InstallError):
            self.install("--hostname", "wealthfolio.example.lan", "--tls-mode", "controller", answers=[])
        key = (self.state / "master-key").read_bytes()
        self.install("--hostname", "wealthfolio.example.lan", answers=[])
        self.install("--configure-access", answers=["", ""])
        self.assertEqual(self.cluster.upgrades, 0)
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        self.assertEqual(self.cluster.secret_creations, 1)

    def test_issued_modes_choose_distinct_names_and_provision_before_authentication(self):
        observed = []
        def provider(instance, namespace):
            self.assertIsNotNone(namespace)
            self.assertIsNone(self.cluster.secret)
            observed.append(instance.tls_options())
            return {"mode": instance.args.tls_mode, "secretName": instance.args.tls_secret}
        with mock.patch.object(installer.Installer, "provision_tls", provider):
            self.install("--hostname", "wealthfolio.example.lan", "--tls-mode", "private-ca", answers=[])
        self.assertEqual(observed[0]["secretName"], "wealthfolio-private-tls")
        self.assertEqual(self.cluster.values["ingress"]["tls"][0]["secretName"], "wealthfolio-private-tls")

    def test_existing_issuer_reference_selects_managed_certificate_mode(self):
        observed = []
        def provider(instance, namespace):
            observed.append(instance.tls_options())
            return {"mode": "cert-manager", "secretName": instance.args.tls_secret}
        with mock.patch.object(installer.Installer, "provision_tls", provider):
            self.install("--hostname", "wealthfolio.example.lan", "--issuer", "private-ca-issuer", "--issuer-kind", "ClusterIssuer", answers=[])
        self.assertEqual(observed, [{"mode": "cert-manager", "secretName": "wealthfolio-managed-tls", "issuerName": "private-ca-issuer", "issuerKind": "ClusterIssuer"}])

    def test_new_certificate_is_not_provisioned_before_configure_confirmation(self):
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        before = (self.state / "state.json").read_bytes()
        with mock.patch.object(installer.Installer, "provision_tls") as provision:
            self.install("--configure-access", "--tls-mode", "private-ca", answers=["no"])
            provision.assert_not_called()
        self.assertEqual((self.state / "state.json").read_bytes(), before)

    def test_private_ca_renewal_calls_helper_without_an_unnecessary_helm_revision(self):
        calls = []
        def provider(instance, namespace):
            calls.append(instance.args.renew_certificate)
            return {"mode": "private-ca", "secretName": instance.args.tls_secret}
        with mock.patch.object(installer.Installer, "provision_tls", provider):
            self.install("--hostname", "wealthfolio.example.lan", "--tls-mode", "private-ca", answers=[])
            self.install("--configure-access", "--tls-mode", "private-ca", "--renew-certificate", answers=["yes"])
        self.assertEqual(calls, [False, True])
        self.assertEqual(self.cluster.upgrades, 0)
        self.assertEqual(self.cluster.release["revision"], "1")
        self.assertTrue(json.loads((self.state / "state.json").read_text())["lastAccessChange"]["certificateRenewed"])

    def test_renewal_never_creates_a_new_ca_for_an_http_installation(self):
        self.install("--hostname", "wealthfolio.example.lan", "--scheme", "http", answers=[])
        with mock.patch.object(installer.Installer, "provision_tls") as provision:
            with self.assertRaisesRegex(installer.InstallError, "existing private-ca installation"):
                self.install("--configure-access", "--tls-mode", "private-ca", "--renew-certificate", answers=[])
            provision.assert_not_called()
        self.assertEqual(self.cluster.upgrades, 0)

    def test_ordinary_private_ca_rerun_exports_only_public_certificate_without_provision(self):
        public = Path(self.temporary.name) / "ca.crt"
        public.write_text("-----BEGIN CERTIFICATE-----\nY2VydGlmaWNhdGU=\n-----END CERTIFICATE-----\n")
        material = {"mode": "private-ca", "secretName": "wealthfolio-private-tls", "publicCaFile": str(public), "publicCaSha256": hashlib.sha256(public.read_bytes()).hexdigest()}
        with mock.patch.object(installer.Installer, "provision_tls", return_value=material):
            self.install("--hostname", "wealthfolio.example.lan", "--tls-mode", "private-ca", answers=[])
        exported = Path(self.temporary.name) / "exported-ca.crt"
        with mock.patch.object(installer.Installer, "provision_tls", side_effect=AssertionError("ordinary rerun must not issue or rotate certificates")):
            self.install("--hostname", "wealthfolio.example.lan", "--export-ca", str(exported), answers=[])
        self.assertEqual(exported.read_bytes(), public.read_bytes())
        self.assertNotIn(public.read_text(), self.output.getvalue())
        fingerprint = hashlib.sha256(b"certificate").hexdigest()
        self.assertIn("SHA-256 certificate fingerprint (DER): " + ":".join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2)), self.output.getvalue())
        self.assertIn("Public CA file SHA256: " + material["publicCaSha256"], self.output.getvalue())
        self.assertEqual(self.cluster.upgrades, 0)

    def test_public_ca_export_rejects_private_key_material_even_with_matching_hash(self):
        private = Path(self.temporary.name) / "private.pem"
        private.write_text("-----BEGIN PRIVATE KEY-----\nc2VjcmV0\n-----END PRIVATE KEY-----\n")
        args = installer.parser().parse_args(["--context", "test", "--local-access", "--export-ca", str(Path(self.temporary.name) / "do-not-export")])
        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/true"):
            instance = installer.Installer(args)
        with self.assertRaisesRegex(installer.InstallError, "public CA certificate did not match"):
            instance.show_public_ca({"publicCaFile": str(private), "publicCaSha256": hashlib.sha256(private.read_bytes()).hexdigest()})
        self.assertFalse(Path(args.export_ca).exists())

    def test_tls_mode_conflicts_fail_before_target_queries(self):
        flags = [
            ["--scheme", "http", "--tls-mode", "controller"],
            ["--tls-mode", "controller", "--tls-secret", "existing-tls"],
            ["--tls-mode", "private-ca", "--issuer", "issuer"],
            ["--renew-certificate"],
        ]
        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/true"), mock.patch.object(installer, "run") as command:
            for extra in flags:
                with self.subTest(extra=extra), self.assertRaises(installer.InstallError):
                    installer.Installer(installer.parser().parse_args(["--context", "test", "--hostname", "wealthfolio.example.lan", *extra]))
            command.assert_not_called()

    def test_generated_certificate_names_are_valid_for_maximum_release_length(self):
        names = []
        for ending in ("b" * 11, "c" * 11):
            args = installer.parser().parse_args(["--context", "test", "--hostname", "wealthfolio.example.lan", "--release", "a" * 42 + ending, "--tls-mode", "private-ca"])
            with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/true"):
                instance = installer.Installer(args)
            instance.choose_network()
            name = instance.args.tls_secret
            self.assertLessEqual(len(name), 63)
            self.assertTrue(installer.NAME.fullmatch(name))
            self.assertEqual(instance.default_certificate_name("-private-tls"), name)
            names.append(name)
        self.assertNotEqual(*names)

    def test_failed_preinstall_issuer_can_switch_to_controller_without_state_reset(self):
        def fail_provider(instance, namespace):
            raise installer.InstallError("Issuer is unavailable; choose controller/private-ca or prepare the issuer.")
        with mock.patch.object(installer.Installer, "provision_tls", fail_provider):
            with self.assertRaisesRegex(installer.InstallError, "Issuer is unavailable"):
                self.install("--hostname", "wealthfolio.example.lan", "--ingress-class", "custom-controller", "--issuer", "missing-issuer", answers=[])
        prepared = json.loads((self.state / "state.json").read_text())
        self.assertEqual(self.cluster.secret_creations, 0)
        self.assertEqual(self.cluster.installations, 0)
        self.install("--hostname", "wealthfolio.example.lan", "--tls-mode", "controller", answers=[])
        completed = json.loads((self.state / "state.json").read_text())
        self.assertEqual(completed["installationId"], prepared["installationId"])
        self.assertEqual(completed["namespaceUid"], prepared["namespaceUid"])
        self.assertEqual(completed["values"]["image"], prepared["values"]["image"])
        self.assertEqual(completed["values"]["encryption"], prepared["values"]["encryption"])
        self.assertEqual(completed["values"]["ingress"]["className"], "custom-controller")
        self.assertEqual(completed["tlsOptions"]["mode"], "controller")
        self.assertNotIn("tlsProvisioningFailed", completed)

    def test_preinstall_network_change_refuses_existing_workload_or_storage(self):
        with mock.patch.object(installer.Installer, "provision_tls", side_effect=installer.InstallError("Issuer unavailable")):
            with self.assertRaises(installer.InstallError):
                self.install("--hostname", "wealthfolio.example.lan", "--issuer", "missing", answers=[])
        self.cluster.deployment = {"metadata": {"uid": "existing-workload"}}
        with self.assertRaisesRegex(installer.InstallError, "workload prevents"):
            self.install("--hostname", "wealthfolio.example.lan", "--tls-mode", "controller", answers=[])
        self.cluster.deployment = None
        self.cluster.claim = {"metadata": {"uid": "existing-storage"}}
        with self.assertRaisesRegex(installer.InstallError, "retained or unrelated claim"):
            self.install("--hostname", "wealthfolio.example.lan", "--tls-mode", "controller", answers=[])
        self.assertEqual(self.cluster.secret_creations, 0)

    def test_preinstall_network_change_reuses_existing_owned_authentication_key(self):
        self.prepare_tls()
        self.cluster.secret_response_lost = True
        with self.assertRaises(installer.InstallError):
            self.install("--hostname", "wealthfolio.example.lan", "--tls-secret", "existing-tls", answers=[])
        key = (self.state / "master-key").read_bytes()
        owner = json.loads((self.state / "state.json").read_text())["installationId"]
        self.install("--hostname", "wealthfolio.example.lan", "--tls-mode", "controller", answers=[])
        self.assertEqual((self.state / "master-key").read_bytes(), key)
        self.assertEqual(json.loads((self.state / "state.json").read_text())["installationId"], owner)
        self.assertEqual(self.cluster.secret_creations, 1)


class CommandCompatibilityTests(unittest.TestCase):
    def test_real_helm3_and_helm4_accept_selected_all_status_flags_without_cluster_access(self):
        for name, expected in (("helm", ["--all"]), ("helm4", [])):
            binary = installer.ROOT / ".tools/bin" / name
            if not binary.is_file():
                self.skipTest("Pinned Helm binaries are not installed")
            with self.subTest(binary=name):
                flags = installer.helm_list_status_flags(binary)
                self.assertEqual(flags, expected)
                # --help exits before contacting a cluster but still parses
                # these flags, reproducing the actual Helm 4 regression.
                result = installer.run([binary, "list", *flags, "--help"])
                self.assertEqual(result.returncode, 0)

    def test_helm_failure_reports_operation_and_safe_category_without_raw_output(self):
        private = "sensitive-synthetic-key-$argon2id$-private"
        for operation, stderr, expected in (
            (["list"], "Error: unknown flag: --all\n" + private, "unsupported option --all"),
            (["get", "values", "private-release"], "Error: forbidden\n" + private, "permission denied"),
            (["upgrade", "private-release"], private, "check target access"),
        ):
            result = subprocess.CompletedProcess([], 1, private, stderr)
            with self.subTest(operation=operation), mock.patch.object(installer.subprocess, "run", return_value=result):
                with self.assertRaises(installer.InstallError) as failure:
                    installer.run(["/usr/local/bin/helm", *operation, "--values", "/private/credentials.json"], data=private)
                message = str(failure.exception)
                self.assertIn("helm " + " ".join(operation[:2] if operation[0] == "get" else operation[:1]), message)
                self.assertIn("exit 1", message)
                self.assertIn(expected, message)
                for protected in (private, "private-release", "/private/credentials.json"):
                    self.assertNotIn(protected, message)

    def test_unverified_helm_major_is_refused(self):
        for version in ("v2.17.0", "v5.0.0", "unrecognized output"):
            with self.subTest(version=version), mock.patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, version, "")):
                with self.assertRaisesRegex(installer.InstallError, "requires Helm 3 or Helm 4"):
                    installer.helm_list_status_flags("helm")


class TargetResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        for patcher in (mock.patch.object(installer.Path, "home", return_value=self.home),
                        mock.patch.dict(installer.os.environ, {"KUBECONFIG": ""}),
                        mock.patch.object(installer.shutil, "which", return_value="/usr/bin/true")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def args(self, *flags):
        return installer.parser().parse_args(["--local-access", *flags])

    def user_config(self):
        config = self.home / ".kube/config"
        config.parent.mkdir(exist_ok=True)
        config.write_text("synthetic private kubeconfig")
        return config

    def test_default_uses_current_context_and_pins_standard_config(self):
        config = self.user_config()
        with mock.patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, "local-cluster\n", "")) as command:
            result = installer.Installer(self.args())
        command.assert_called_once_with(["/usr/bin/true", "--kubeconfig", str(config), "config", "current-context"], check=False)
        self.assertEqual(result.context, "local-cluster")
        self.assertEqual(result.kubectl[-2:], ["--context", "local-cluster"])
        self.assertEqual(result.helm_target, ["--kubeconfig", str(config), "--kube-context", "local-cluster"])

    def test_explicit_context_overrides_current_context(self):
        self.user_config()
        with mock.patch.object(installer, "run") as command:
            result = installer.Installer(self.args("--context", "chosen-cluster"))
        self.assertEqual(result.context, "chosen-cluster")
        command.assert_not_called()

    def test_kubeconfig_alone_uses_its_current_context(self):
        config = self.home / "custom cluster.yaml"
        config.write_text("synthetic selected config")
        with mock.patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, "custom-context\n", "")) as command:
            result = installer.Installer(self.args("--kubeconfig", str(config)))
        command.assert_called_once_with(["/usr/bin/true", "--kubeconfig", str(config), "config", "current-context"], check=False)
        self.assertEqual(result.context, "custom-context")

    def test_current_context_name_is_preserved_exactly(self):
        self.user_config()
        with mock.patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, " context with spaces \r\n", "")):
            result = installer.Installer(self.args())
        self.assertEqual(result.context, " context with spaces ")
        self.assertEqual(result.kubectl[-1], " context with spaces ")

    def test_merged_kubeconfig_environment_is_preserved_without_single_file_override(self):
        paths = str(self.home / "missing") + os.pathsep + str(self.user_config())
        with mock.patch.dict(installer.os.environ, {"KUBECONFIG": paths}), mock.patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, "merged-context\n", "")) as command:
            result = installer.Installer(self.args())
        command.assert_called_once_with(["/usr/bin/true", "config", "current-context"], check=False)
        self.assertNotIn("--kubeconfig", result.kubectl)
        self.assertEqual(result.context, "merged-context")

    def test_missing_invalid_or_unreadable_configuration_never_selects_another_source(self):
        for source in ("missing-environment-file", "empty-default-file", "unreadable-default-file", "broken-default-symlink"):
            with self.subTest(source=source):
                config = self.home / ".kube/config"
                if config.exists() or config.is_symlink():
                    config.unlink()
                environment = {"KUBECONFIG": str(self.home / "missing")} if source == "missing-environment-file" else {"KUBECONFIG": ""}
                if source != "missing-environment-file":
                    config = self.user_config()
                    if source == "empty-default-file":
                        config.write_text("")
                    elif source == "unreadable-default-file":
                        config.chmod(0o000)
                    else:
                        config.unlink()
                        config.symlink_to(self.home / "missing")
                args = self.args()
                with mock.patch.dict(installer.os.environ, environment), mock.patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 1, "", "private diagnostic")):
                    with self.assertRaisesRegex(installer.InstallError, "no other target was selected"):
                        installer.Installer(args)

    def test_absent_or_ambiguous_current_context_fails_without_mutation(self):
        self.user_config()
        for output in ("", "first\nsecond\n"):
            with self.subTest(output=output), mock.patch.object(installer, "run", return_value=subprocess.CompletedProcess([], 0, output, "")) as command:
                with self.assertRaisesRegex(installer.InstallError, "No usable current Kubernetes context"):
                    installer.Installer(self.args())
                self.assertEqual(command.call_count, 1)
        with mock.patch.object(installer, "run") as command:
            with self.assertRaisesRegex(installer.InstallError, "selected kubeconfig file"):
                installer.Installer(self.args("--kubeconfig", str(self.home / "missing")))
            command.assert_not_called()

    def test_unconfigured_client_stops_before_invoking_a_client_specific_fallback(self):
        with mock.patch.object(installer, "run") as command:
            with self.assertRaisesRegex(installer.InstallError, "No usable default kubeconfig"):
                installer.Installer(self.args())
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
