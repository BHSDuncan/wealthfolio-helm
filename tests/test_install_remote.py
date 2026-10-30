"""SSH setup transfer integrity and explicit-target safety, without a cluster."""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shlex
import ssl
import subprocess
import sys
import tarfile
from unittest import mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install-remote.py"
SPEC = importlib.util.spec_from_file_location("install_remote", SCRIPT)
remote = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(remote)


def fixture_source(directory):
    for name in (*remote.CORE_FILES, "charts/wealthfolio/Chart.yaml", "charts/wealthfolio/templates/deployment.yaml"):
        file = directory / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("public source for " + name)
    return directory


def args(**changes):
    values = dict(ssh="operator@node.example", identity=None, namespace="wealthfolio", release="wealthfolio",
                  port=8088, state_dir=None, no_access=False, access_only=False,
                  hostname="wealthfolio.example.lan", local_access=False, ingress_class=None, tls_secret=None, scheme=None,
                  configure_access=False, tls_mode=None, issuer=None, issuer_kind=None, export_ca=None,
                  renew_certificate=False, setup_cert_manager=False, email=None, issuer_file=None,
                  install_controller=False, dry_run=False, encrypted=False, existing_secret=None,
                  storage_class=None, context=None, kubeconfig=None, ingress_port=None,
                  image_repository=None, image_tag=None, image_digest=None, listen_address=None)
    return argparse.Namespace(**(values | changes))


@pytest.mark.parametrize("target", ["-oProxyCommand=evil", "host;touch /tmp/invalid", "user@host\nother", "user@host:22", "$(whoami)"])
def test_reject_ssh_option_and_shell_injection(target):
    with pytest.raises(ValueError):
        remote.validate_args(args(ssh=target))


def test_quoted_paths_are_literal_arguments():
    options = args(state_dir="/home/operator/private state; do-not-run")
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert command[:3] == ["sudo", "--", "bash"]
    assert command[command.index("--state-dir") + 1] == options.state_dir
    assert command[command.index("--hostname") + 1] == options.hostname
    assert "--access" not in command
    assert "--scheme" not in command and "--yes" not in command
    assert "-c" not in command


def test_access_only_and_install_without_access_are_distinct():
    access = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", args(access_only=True, local_access=True, hostname=None)))
    install = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", args(no_access=True, local_access=True, hostname=None)))
    assert "--access-only" in access and "--access" not in access
    assert "--access" not in install and "--access-only" not in install
    assert "--no-access" in install


def test_operator_settings_are_forwarded_without_shell_interpolation():
    options = args(encrypted=True, storage_class="local-path", existing_secret="my-auth")
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert "--encrypted" in command
    assert command[command.index("--existing-secret") + 1] == "my-auth"
    assert command[command.index("--storage-class") + 1] == "local-path"


def test_custom_remote_target_network_and_mirror_settings_are_literal_arguments():
    options = args(context="edge-target", kubeconfig="/srv/cluster configs/target.yaml",
                   ingress_port=8443, scheme="https", tls_mode="controller",
                   image_repository="registry.example.test:5443/wealthfolio", image_tag="3.11.0",
                   image_digest="sha256:" + "a" * 64, listen_address="::")
    remote.validate_args(options)
    command = shlex.split(remote.remote_command("/srv/staging/wealthfolio-install-safe/payload", "operator", options))
    for key in ("context", "kubeconfig", "ingress_port", "image_repository", "image_tag", "image_digest", "listen_address"):
        assert command[command.index("--" + key.replace("_", "-")) + 1] == str(getattr(options, key))
    assert "--local-k3s" in command and "-c" not in command
    options.setup_cert_manager = True
    options.hostname = options.scheme = options.tls_mode = options.ingress_port = None
    options.image_repository = options.image_tag = options.image_digest = options.listen_address = None
    options.issuer = "public-acme"
    options.email = "operator@example.test"
    remote.validate_args(options)
    setup = shlex.split(remote.remote_command("/srv/staging/wealthfolio-install-safe/payload", "operator", options))
    assert setup[setup.index("--context") + 1] == options.context
    assert setup[setup.index("--kubeconfig") + 1] == options.kubeconfig


def test_remote_install_preserves_explicit_default_ingress_class_selection():
    options = args(ingress_class="")
    remote.validate_args(options)
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert command[command.index("--ingress-class") + 1] == ""


@pytest.mark.parametrize("changes", [{"kubeconfig": "relative/target.yaml"},
                                     {"image_repository": "registry.example.test/wealthfolio"},
                                     {"ingress_port": 0}, {"ingress_port": 65536},
                                     {"local_access": True, "hostname": None, "ingress_port": 8443}])
def test_invalid_network_target_options_fail_before_ssh(changes):
    with pytest.raises(ValueError):
        remote.validate_args(args(**changes))


def test_remote_probe_respects_tmpdir_and_protects_staging(tmp_path):
    result = subprocess.run([sys.executable, "-c", "import platform; platform.system=lambda:'Linux';" + remote.PROBE_REMOTE],
                            env=dict(remote.os.environ, TMPDIR=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    directory = Path(json.loads(result.stdout)["directory"])
    try:
        assert directory.parent == tmp_path
        assert directory.name.startswith("wealthfolio-install-")
        assert directory.stat().st_mode & 0o777 == 0o700
    finally:
        remote.shutil.rmtree(directory)


def test_remote_main_accepts_private_staging_outside_tmp_and_uses_local_tmpdir(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "repo")
    local_tmp = tmp_path / "private temporary"
    local_tmp.mkdir()
    staging = "/srv/custom staging/wealthfolio-install-safe"
    monkeypatch.setattr(remote, "ROOT", source)
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setenv("TMPDIR", str(local_tmp))
    monkeypatch.setattr(remote.tempfile, "tempdir", None)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if any("import importlib.util,json" in item for item in command):
            stdout = json.dumps({"user": "operator", "directory": staging})
        elif any("Setup transfer checksum mismatch" in item for item in command):
            stdout = staging + "/payload\n"
        elif any("Public CA export must contain" in item for item in command):
            stdout = json.dumps({"exists": False})
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(remote.subprocess, "run", run)
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        assert remote.main(["--ssh", "operator@node.example", "--hostname", "wealthfolio.example.lan"]) == 0
    control = next(item for item in calls[0] if item.startswith("ControlPath="))
    assert Path(control.split("=", 1)[1]).parent.parent == local_tmp
    transfer = next(command for command in calls if command[0] == "scp")
    assert transfer[-1] == "operator@node.example:" + staging + "/setup.tar"
    cleanup = next(command for command in calls if "shutil.rmtree" in str(command))
    assert shlex.split(cleanup[-1])[-1] == staging


def test_bundle_excludes_recovery_keys_and_development_noise(tmp_path):
    source = fixture_source(tmp_path / "repo")
    for name in ("dist/private.json", ".local/key", "SPEC.md", "INSTRUCTIONS.md",
                 "charts/wealthfolio/.env", "charts/wealthfolio/private.key",
                 "charts/wealthfolio/wealthfolio-0.1.0.tgz", "charts/wealthfolio/values.yaml.un~",
                 "charts/wealthfolio/templates/.private.yaml", "charts/wealthfolio/templates/key.pem"):
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("must not transfer")
    temporary = tmp_path / "work"
    temporary.mkdir()
    archive, checksum = remote.create_bundle(source, temporary)
    assert remote.sha256(archive) == checksum
    with tarfile.open(archive) as bundle:
        manifest = json.load(bundle.extractfile("TRANSFER.json"))
        assert set(manifest["files"]) == set(remote.CORE_FILES) | {"charts/wealthfolio/Chart.yaml", "charts/wealthfolio/templates/deployment.yaml"}
        assert all(entry["sha256"] for entry in manifest["files"].values())


def test_transfer_includes_every_current_public_chart_source(tmp_path):
    chart = remote.ROOT / "charts/wealthfolio"
    expected = {path for path in chart.rglob("*") if path.is_file() and path.name != ".DS_Store"}
    actual = {path for path in remote.source_files(remote.ROOT) if path.is_relative_to(chart)}
    assert actual == expected


def test_source_symlink_is_not_followed(tmp_path):
    source = fixture_source(tmp_path / "repo")
    secret = tmp_path / "private-key"
    secret.write_text("secret")
    (source / "charts/wealthfolio/templates/key").symlink_to(secret)
    with pytest.raises(ValueError, match="symlinks"):
        remote.source_files(source)


def extract(archive, destination, checksum):
    destination.mkdir(mode=0o700)
    (destination / "setup.tar").write_bytes(archive.read_bytes())
    return subprocess.run([sys.executable, "-c", remote.EXTRACT_REMOTE, str(destination), checksum], capture_output=True, text=True)


def test_actual_transfer_verifier_checks_every_file(tmp_path):
    source = fixture_source(tmp_path / "repo")
    temporary = tmp_path / "work"
    temporary.mkdir()
    archive, checksum = remote.create_bundle(source, temporary)
    result = extract(archive, tmp_path / "destination", checksum)
    assert result.returncode == 0, result.stderr
    copied = tmp_path / "destination/payload/scripts/install.sh"
    assert copied.read_bytes() == (source / "scripts/install.sh").read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o700


def test_transfer_checksum_failure_occurs_before_extraction(tmp_path):
    source = fixture_source(tmp_path / "repo")
    temporary = tmp_path / "work"
    temporary.mkdir()
    archive, _ = remote.create_bundle(source, temporary)
    result = extract(archive, tmp_path / "destination", "0" * 64)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr
    assert not (tmp_path / "destination/payload").exists()


@pytest.mark.parametrize("name,kind", [("charts/wealthfolio/../../outside", "file"), ("charts/wealthfolio/link", "symlink")])
def test_transfer_cannot_escape_or_follow_tar_links(tmp_path, name, kind):
    archive = tmp_path / "malicious.tar"
    files = {item: {"sha256": hashlib.sha256(b"").hexdigest(), "size": 0} for item in remote.CORE_FILES}
    files[name] = {"sha256": hashlib.sha256(b"").hexdigest(), "size": 0}
    with tarfile.open(archive, "w") as bundle:
        manifest = json.dumps({"schemaVersion": 1, "files": files}).encode()
        meta = tarfile.TarInfo("TRANSFER.json")
        meta.size = len(manifest)
        bundle.addfile(meta, io.BytesIO(manifest))
        for entry in files:
            member = tarfile.TarInfo(entry)
            if entry == name and kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "/tmp/outside"
            bundle.addfile(member, io.BytesIO(b""))
    result = extract(archive, tmp_path / "destination", remote.sha256(archive))
    assert result.returncode != 0
    assert "Unsafe setup member" in result.stderr
    assert not (tmp_path / "destination/outside").exists()


@pytest.mark.parametrize("mode,private_forward", [
    (["--hostname", "wealthfolio.example.lan"], False),
    (["--hostname", "wealthfolio.example.lan", "--scheme", "https"], False),
    (["--hostname", "wealthfolio.example.lan", "--tls-secret", "wealthfolio-tls"], False),
    (["--local-access"], True),
])
def test_main_keeps_identity_contents_and_credentials_out_of_bundle(tmp_path, monkeypatch, mode, private_forward):
    source = fixture_source(tmp_path / "repo")
    key = tmp_path / "ssh key"
    key.write_text("PRIVATE KEY NEVER READ")
    monkeypatch.setattr(remote, "ROOT", source)
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if any("import importlib.util,json" in item for item in command):
            stdout = json.dumps({"user": "operator", "directory": "/tmp/wealthfolio-install-safe"})
        elif any("Setup transfer checksum mismatch" in item for item in command):
            stdout = "/tmp/wealthfolio-install-safe/payload\n"
        elif any("Public CA export must contain" in item for item in command):
            stdout = json.dumps({"exists": False})
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    original_open = Path.open

    def protected_open(path, *positional, **keywords):
        assert path != key, "private key must be passed to ssh, never read"
        return original_open(path, *positional, **keywords)

    monkeypatch.setattr(remote.subprocess, "run", run)
    monkeypatch.setattr(Path, "open", protected_open)
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        assert remote.main(["--ssh", "operator@node.example", "--identity", str(key), *mode]) == 0
    sessions = [command for command in calls if "-t" in command]
    assert len(sessions) == 1
    assert ("127.0.0.1:8088:127.0.0.1:8088" in sessions[0]) is private_forward
    assert ("--access" in shlex.split(sessions[0][-1])) is private_forward
    if not private_forward:
        command = shlex.split(sessions[0][-1])
        assert command[command.index("--hostname") + 1] == "wealthfolio.example.lan"
        if "--tls-secret" in mode:
            assert command[command.index("--tls-secret") + 1] == "wealthfolio-tls"
        if "--scheme" not in mode:
            assert "--scheme" not in command, "Unspecified scheme must reach the core as unspecified"
    assert not any("PRIVATE KEY NEVER READ" in str(command) for command in calls)
    assert any("-O" in command and "exit" in command for command in calls)


def test_invalid_remote_path_is_never_cleaned_up(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "repo")
    monkeypatch.setattr(remote, "ROOT", source)
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"user": "operator", "directory": "/home/operator"}), stderr="")

    monkeypatch.setattr(remote.subprocess, "run", run)
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        assert remote.main(["--ssh", "operator@node.example", "--hostname", "wealthfolio.example.lan"]) == 1
    assert not any("shutil.rmtree" in str(command) for command in calls)


@pytest.mark.parametrize("hostname", ["https://wealthfolio.lan", "wealthfolio.lan/path", "192.0.2.20", "*.example.lan", "user@host"])
def test_ingress_hostname_is_a_plain_dns_name(hostname):
    with pytest.raises(ValueError):
        remote.validate_args(args(hostname=hostname))


def test_access_modes_and_tls_are_explicit():
    with pytest.raises(ValueError, match="Choose --hostname"):
        remote.validate_args(args(hostname=None))
    with pytest.raises(ValueError, match="Choose --hostname"):
        remote.validate_args(args(local_access=True))
    with pytest.raises(ValueError, match="require --hostname"):
        remote.validate_args(args(hostname=None, local_access=True, tls_secret="wealthfolio-tls"))
    options = args(hostname=None, access_only=True)
    remote.validate_args(options)
    assert options.local_access


@pytest.mark.parametrize("options", [
    {"scheme": "http", "tls_secret": "wealthfolio-tls"},
    {"hostname": None, "local_access": True, "scheme": "http"},
])
def test_conflicting_scheme_and_tls_fail_before_ssh(options):
    with pytest.raises(ValueError):
        remote.validate_args(args(**options))


def test_explicit_https_scheme_and_tls_reference_are_forwarded():
    options = args(scheme="https", tls_secret="wealthfolio-tls", ingress_class="traefik")
    remote.validate_args(options)
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert command[command.index("--scheme") + 1] == "https"
    assert command[command.index("--tls-secret") + 1] == "wealthfolio-tls"
    assert command[command.index("--ingress-class") + 1] == "traefik"
    assert "--access" not in command and "--local-access" not in command


def test_https_without_secret_reaches_interactive_core_prompt():
    options = args(scheme="https")
    remote.validate_args(options)
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert command[command.index("--scheme") + 1] == "https"
    assert "--tls-secret" not in command


def test_noninteractive_transport_preserves_explicit_settings_without_allocating_tty(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "repo")
    monkeypatch.setattr(remote, "ROOT", source)
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if any("import importlib.util,json" in item for item in command):
            stdout = json.dumps({"user": "operator", "directory": "/tmp/wealthfolio-install-safe"})
        elif any("Setup transfer checksum mismatch" in item for item in command):
            stdout = "/tmp/wealthfolio-install-safe/payload\n"
        elif any("Public CA export must contain" in item for item in command):
            stdout = json.dumps({"exists": False})
        else:
            stdout = ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(remote.subprocess, "run", run)
    with mock.patch.object(sys.stdin, "isatty", return_value=False):
        assert remote.main(["--ssh", "operator@node.example", "--hostname", "wealthfolio.example.lan",
                            "--scheme", "http", "--existing-secret", "operator-auth"]) == 0
    sessions = [command for command in calls if "sudo -n -- bash" in command[-1]]
    assert len(sessions) == 1
    session = sessions[0]
    assert "-T" in session and "-t" not in session and "BatchMode=yes" in session
    command = shlex.split(session[-1])
    assert command[command.index("--scheme") + 1] == "http"
    assert command[command.index("--existing-secret") + 1] == "operator-auth"
    assert "--yes" not in command


def test_noninteractive_transport_never_infers_a_missing_scheme():
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", args(), interactive=False))
    assert command[:3] == ["sudo", "-n", "--"]
    assert "--scheme" not in command and "--tls-secret" not in command


def test_configure_access_is_explicit_and_can_reuse_saved_hostname():
    options = args(hostname=None, configure_access=True)
    remote.validate_args(options)
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert "--configure-access" in command
    assert "--hostname" not in command and "--scheme" not in command
    assert "--local-access" not in command and "--access" not in command
    ordinary = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", args()))
    assert "--configure-access" not in ordinary


@pytest.mark.parametrize("mode", ["controller", "secret", "cert-manager", "private-ca"])
def test_tls_bootstrap_modes_and_public_export_path_are_forwarded(mode):
    options = args(tls_mode=mode)
    remote.validate_args(options)
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert command[command.index("--tls-mode") + 1] == mode
    assert command[command.index("--export-ca") + 1] == "/tmp/wealthfolio-install-safe/public-ca.crt"
    assert "--scheme" not in command, "The core derives HTTPS from the explicit TLS mode"


def test_issuer_and_explicit_leaf_renewal_are_not_inferred():
    options = args(tls_mode="cert-manager", issuer="existing-ca", issuer_kind="ClusterIssuer")
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert command[command.index("--issuer") + 1] == "existing-ca"
    assert command[command.index("--issuer-kind") + 1] == "ClusterIssuer"
    assert "--renew-certificate" not in command
    with pytest.raises(ValueError, match="requires --configure-access"):
        remote.validate_args(args(renew_certificate=True))
    configured = args(hostname=None, configure_access=True, renew_certificate=True)
    remote.validate_args(configured)
    assert "--renew-certificate" in shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", configured))


def public_ca():
    data = (Path(__file__).parent / "fixtures/public-ca.pem").read_bytes()
    return data, {"sha256": hashlib.sha256(data).hexdigest(), "fingerprint": hashlib.sha256(ssl.PEM_cert_to_DER_cert(data.decode())).hexdigest()}


def test_public_ca_export_is_validated_atomic_and_idempotent(tmp_path):
    data, info = public_ca()
    source = tmp_path / "download.pem"
    source.write_bytes(data)
    target = tmp_path / "owner-state/ca.pem"
    assert remote.save_public_ca(source, target, info) == info["fingerprint"]
    inode = target.stat().st_ino
    assert target.read_bytes() == data and target.stat().st_mode & 0o777 == 0o600
    assert remote.save_public_ca(source, target, info) == info["fingerprint"]
    assert target.stat().st_ino == inode
    target.write_text("unrelated public file")
    with pytest.raises(ValueError, match="different file"):
        remote.save_public_ca(source, target, info)
    assert target.read_text() == "unrelated public file"


def test_private_key_can_never_be_exported_as_public_ca(tmp_path):
    private = b"-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n"
    source = tmp_path / "download.pem"
    source.write_bytes(private)
    target = tmp_path / "export.pem"
    info = {"sha256": hashlib.sha256(private).hexdigest(), "fingerprint": "0" * 64}
    with pytest.raises(ValueError, match="private material"):
        remote.save_public_ca(source, target, info)
    assert not target.exists()
    result = subprocess.run([sys.executable, "-c", remote.PUBLIC_CA_REMOTE, str(source)], capture_output=True, text=True)
    assert result.returncode != 0 and not result.stdout.strip(), "Remote rejection occurs before a certificate SCP"
    assert private.decode() not in result.stderr


def test_ca_export_rejects_corruption_and_does_not_follow_output_symlink(tmp_path):
    data, info = public_ca()
    source = tmp_path / "download.pem"
    source.write_bytes(data)
    target = tmp_path / "ca.pem"
    with pytest.raises(ValueError, match="checksum mismatch"):
        remote.save_public_ca(source, target, {**info, "sha256": "0" * 64})
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("keep")
    target.symlink_to(unrelated)
    with pytest.raises(ValueError, match="symlink"):
        remote.save_public_ca(source, target, info)
    assert unrelated.read_text() == "keep"


def test_cert_manager_setup_is_a_separate_explicit_remote_action():
    options = args(hostname=None, setup_cert_manager=True, issuer="public-acme", email="operator@example.test")
    remote.validate_args(options)
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert command[2:5] == ["bash", "/tmp/wealthfolio-install-safe/payload/scripts/setup-cert-manager.sh", "--local-k3s"]
    assert "--install-controller" not in command and "--export-ca" not in command
    assert all(not item.endswith("scripts/install.sh") for item in command)
    options.install_controller = True
    assert "--install-controller" in shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    with pytest.raises(ValueError, match="separate action"):
        remote.validate_args(args(setup_cert_manager=True, issuer="public-acme", email="operator@example.test"))
    with pytest.raises(ValueError, match="Use --setup-cert-manager"):
        remote.validate_args(args(install_controller=True))


def test_reviewed_issuer_input_is_transferred_privately_and_never_as_a_command_value(tmp_path):
    source = fixture_source(tmp_path / "repo")
    issuer = tmp_path / "reviewed.yaml"
    issuer.write_text(json.dumps({"apiVersion":"cert-manager.io/v1","kind":"Issuer","metadata":{"name":"selected","namespace":"wealthfolio"},"spec":{"selfSigned":{}}}))
    options = args(hostname=None, setup_cert_manager=True, issuer="selected", issuer_file=str(issuer))
    remote.validate_args(options)
    temporary = tmp_path / "work"
    temporary.mkdir()
    archive, checksum = remote.create_bundle(source, temporary, issuer)
    result = extract(archive, tmp_path / "destination", checksum)
    assert result.returncode == 0, result.stderr
    transferred = tmp_path / "destination/payload/issuer-input.json"
    assert transferred.read_bytes() == issuer.read_bytes() and transferred.stat().st_mode & 0o777 == 0o600
    command = shlex.split(remote.remote_command("/tmp/wealthfolio-install-safe/payload", "operator", options))
    assert command[command.index("--issuer-file") + 1] == "/tmp/wealthfolio-install-safe/payload/issuer-input.json"
    assert str(issuer) not in command and issuer.read_text() not in str(command)


def test_private_key_is_not_allowed_as_reviewed_issuer_input(tmp_path):
    source = fixture_source(tmp_path / "repo")
    key = tmp_path / "wrong-input.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----")
    temporary = tmp_path / "work"
    temporary.mkdir()
    with pytest.raises(ValueError, match="private-key material"):
        remote.create_bundle(source, temporary, key)


def test_ca_export_downloads_only_the_validated_public_certificate(tmp_path, monkeypatch):
    source = fixture_source(tmp_path / "repo")
    output = tmp_path / "client-state/root-ca.pem"
    certificate, info = public_ca()
    monkeypatch.setattr(remote, "ROOT", source)
    monkeypatch.setattr(remote.shutil, "which", lambda name: "/usr/bin/" + name)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if any("import importlib.util,json" in item for item in command):
            stdout = json.dumps({"user": "operator", "directory": "/tmp/wealthfolio-install-safe"})
        elif any("Setup transfer checksum mismatch" in item for item in command):
            stdout = "/tmp/wealthfolio-install-safe/payload\n"
        elif any("Public CA export must contain" in item for item in command):
            stdout = json.dumps({"exists": True, **info})
        else:
            stdout = ""
        if command[0] == "scp" and command[-2].endswith("/public-ca.crt"):
            Path(command[-1]).write_bytes(certificate)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(remote.subprocess, "run", run)
    with mock.patch.object(sys.stdin, "isatty", return_value=True):
        assert remote.main(["--ssh", "operator@node.example", "--hostname", "wealthfolio.example.lan",
                            "--tls-mode", "private-ca", "--export-ca", str(output)]) == 0
    assert output.read_bytes() == certificate and output.stat().st_mode & 0o777 == 0o600
    transfers = [command for command in calls if command[0] == "scp"]
    assert len(transfers) == 2
    assert transfers[1][-2] == "operator@node.example:/tmp/wealthfolio-install-safe/public-ca.crt"
    assert all("key.pem" not in str(command) and "ca-key" not in str(command) for command in transfers)


@pytest.mark.parametrize("kind", ["Secret", "List", "ClusterIssuer", "ClusterRole"])
def test_reviewed_issuer_wrong_kind_is_refused_before_any_ssh(tmp_path, monkeypatch, kind):
    supplied = tmp_path / "invalid.json"
    supplied.write_text(json.dumps({"apiVersion": "cert-manager.io/v1", "kind": kind,
                                   "metadata": {"name": "chosen", "namespace": "wealthfolio"}, "spec": {"selfSigned": {}}}))
    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **k: pytest.fail("Invalid issuer must not contact SSH"))
    assert remote.main(["--ssh", "operator@node.example", "--setup-cert-manager", "--issuer", "chosen",
                        "--issuer-file", str(supplied), "--install-controller"]) == 1
