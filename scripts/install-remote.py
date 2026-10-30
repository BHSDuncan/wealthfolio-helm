#!/usr/bin/env python3
"""Run the local K3s installer over a verified SSH connection."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CORE_FILES = (
    "scripts/install.sh",
    "scripts/installer.py",
    "scripts/installer-requirements.lock",
    "scripts/prepare-python.sh",
    "scripts/tls_bootstrap.py",
    "scripts/setup-cert-manager.sh",
    "scripts/cert-manager.lock.json",
    "tools.lock.json",
    "LICENSE",
    "NOTICE",
)
CHART_FILES = {"Chart.yaml", "values.yaml", "values.schema.json", "README.md", "LICENSE", "NOTICE", ".helmignore"}
TEMPLATE_SUFFIXES = {".yaml", ".yml", ".tpl", ".txt"}
TARGET = re.compile(r"(?:[A-Za-z_][A-Za-z0-9_.-]*@)?(?:[A-Za-z0-9][A-Za-z0-9_.-]*|\[[A-Za-z0-9:.%_-]+\])\Z")
DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
HOSTNAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z")

# Never extract a supplied tar path, link, owner, or mode directly. This runs as
# the SSH account, before sudo, against our fresh private temporary directory.
EXTRACT_REMOTE = r'''
import hashlib,json,os,pathlib,sys,tarfile
base=pathlib.Path(sys.argv[1]); expected=sys.argv[2]
archive=base/'setup.tar'
if not base.is_dir() or base.is_symlink() or base.stat().st_uid!=os.getuid():
    raise SystemExit('Unsafe setup directory')
digest=hashlib.sha256()
with archive.open('rb') as source:
    while chunk:=source.read(1048576): digest.update(chunk)
if digest.hexdigest()!=expected:
    raise SystemExit('Setup transfer checksum mismatch')
payload=base/'payload'; payload.mkdir(mode=0o700)
required={'scripts/install.sh','scripts/installer.py','scripts/installer-requirements.lock','scripts/prepare-python.sh','scripts/tls_bootstrap.py','scripts/setup-cert-manager.sh','scripts/cert-manager.lock.json','tools.lock.json','LICENSE','NOTICE'}
allowed=required|{'issuer-input.json'}
with tarfile.open(archive,'r:') as bundle:
    members=bundle.getmembers()
    if len(members)>2000 or len({m.name for m in members})!=len(members):
        raise SystemExit('Invalid setup file list')
    meta=bundle.getmember('TRANSFER.json')
    if not meta.isfile() or meta.size>131072:
        raise SystemExit('Invalid setup manifest')
    manifest=json.load(bundle.extractfile(meta))
    if manifest.get('schemaVersion')!=1 or not isinstance(manifest.get('files'),dict):
        raise SystemExit('Invalid setup manifest')
    files=manifest['files']
    if set(files)!={m.name for m in members if m.name!='TRANSFER.json'}:
        raise SystemExit('Setup manifest does not match files')
    if not required.issubset(files) or not any(n.startswith('charts/wealthfolio/') for n in files):
        raise SystemExit('Missing installer source')
    for member in members:
        if member.name=='TRANSFER.json': continue
        name=pathlib.PurePosixPath(member.name)
        if (name.is_absolute() or '..' in name.parts or not member.isfile()
            or (member.name not in allowed and not member.name.startswith('charts/wealthfolio/'))
            or member.size>16777216 or member.size!=files[member.name].get('size')):
            raise SystemExit('Unsafe setup member')
        target=payload.joinpath(*name.parts)
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        digest=hashlib.sha256()
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o700 if member.name.endswith('.sh') else 0o600)
        with os.fdopen(fd,'wb') as output, bundle.extractfile(member) as source:
            while chunk:=source.read(1048576):
                digest.update(chunk); output.write(chunk)
        if digest.hexdigest()!=files[member.name].get('sha256'):
            raise SystemExit('Setup file checksum mismatch')
print(str(payload))
'''

PROBE_REMOTE = r'''
import importlib.util,json,os,platform,pwd,sys,tempfile
if sys.version_info<(3,10) or platform.system()!='Linux':
    raise SystemExit('The K3s node needs Linux and Python 3.10 or newer')
if importlib.util.find_spec('venv') is None:
    raise SystemExit('Install the Python venv package on the K3s node first')
account=pwd.getpwuid(os.getuid())
directory=tempfile.mkdtemp(prefix='wealthfolio-install-')
os.chmod(directory,0o700)
print(json.dumps({'user':account.pw_name,'directory':directory}))
'''

PUBLIC_CA_REMOTE = r'''
import hashlib,json,pathlib,ssl,stat,sys
p=pathlib.Path(sys.argv[1])
if not p.exists():
    print(json.dumps({'exists':False}));raise SystemExit(0)
if p.is_symlink() or not stat.S_ISREG(p.stat().st_mode) or p.stat().st_size>65536:
    raise SystemExit('Unsafe public CA export')
data=p.read_bytes();text=data.decode('ascii')
if 'PRIVATE KEY' in text or text.count('-----BEGIN CERTIFICATE-----')!=1:
    raise SystemExit('Public CA export must contain only one certificate, never a private key')
ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=text)
der=ssl.PEM_cert_to_DER_cert(text)
print(json.dumps({'exists':True,'sha256':hashlib.sha256(data).hexdigest(),'fingerprint':hashlib.sha256(der).hexdigest()}))
'''


def sha256(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_files(root: Path) -> list[Path]:
    files = [root / entry for entry in CORE_FILES]
    chart = root / "charts/wealthfolio"
    if chart.is_symlink() or not chart.is_dir():
        raise ValueError("Chart source is missing or is a symlink")
    for candidate in chart.rglob("*"):
        if candidate.is_symlink():
            raise ValueError("Installer source must not contain symlinks")
        relative = candidate.relative_to(chart)
        metadata = relative.as_posix() in CHART_FILES
        template = (relative.parts[0] == "templates" and candidate.suffix in TEMPLATE_SUFFIXES
                    and not any(part.startswith(".") for part in relative.parts))
        if candidate.is_file() and (metadata or template):
            files.append(candidate)
    for candidate in files:
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"Required installer source is missing: {candidate.relative_to(root)}")
    return sorted(files)


def create_bundle(root: Path, directory: Path, issuer_file: Path | None = None) -> tuple[Path, str]:
    """Snapshot only public installer source; never include local state/caches."""
    snapshot = directory / "source"
    snapshot.mkdir(mode=0o700)
    manifest: dict = {"schemaVersion": 1, "files": {}}
    for source in source_files(root):
        relative = source.relative_to(root)
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, target, follow_symlinks=False)
        target.chmod(0o700 if target.suffix == ".sh" else 0o600)
        manifest["files"][relative.as_posix()] = {"sha256": sha256(target), "size": target.stat().st_size}
    if issuer_file:
        target = snapshot / "issuer-input.json"
        shutil.copyfile(issuer_file, target, follow_symlinks=False)
        target.chmod(0o600)
        if b"PRIVATE KEY" in target.read_bytes():
            raise ValueError("The reviewed Issuer file must not contain private-key material")
        manifest["files"]["issuer-input.json"] = {"sha256": sha256(target), "size": target.stat().st_size}
    (snapshot / "TRANSFER.json").write_text(json.dumps(manifest, sort_keys=True))
    (snapshot / "TRANSFER.json").chmod(0o600)
    archive = directory / "setup.tar"
    with tarfile.open(archive, "w") as bundle:
        for relative in sorted([*manifest["files"], "TRANSFER.json"]):
            bundle.add(snapshot / relative, arcname=relative, recursive=False)
    archive.chmod(0o600)
    return archive, sha256(archive)


def validate_args(args: argparse.Namespace) -> None:
    if not TARGET.fullmatch(args.ssh):
        raise ValueError("Use an SSH host alias or user@host, without shell options")
    if not DNS_LABEL.fullmatch(args.namespace) or len(args.namespace) > 63:
        raise ValueError("Namespace must be a Kubernetes DNS label")
    if not DNS_LABEL.fullmatch(args.release) or len(args.release) > 42:
        raise ValueError("Release must be a DNS label of at most 42 characters")
    if not 1024 <= args.port <= 65535:
        raise ValueError("Local access port must be between 1024 and 65535")
    if args.identity:
        args.identity = str(Path(args.identity).expanduser().resolve())
        if not Path(args.identity).is_file():
            raise ValueError("SSH identity file does not exist")
    if args.state_dir and not args.state_dir.startswith("/"):
        raise ValueError("Remote state directory must be an absolute path")
    if args.kubeconfig and not Path(args.kubeconfig).is_absolute():
        raise ValueError("Remote kubeconfig must be an absolute path on the K3s host")
    if bool(args.image_repository) != bool(args.image_digest):
        raise ValueError("Custom images require both --image-repository and --image-digest")
    if args.ingress_port is not None and not 1 <= args.ingress_port <= 65535:
        raise ValueError("External Ingress port must be between 1 and 65535")
    if args.setup_cert_manager:
        if any((args.hostname, args.local_access, args.configure_access, args.access_only,
                args.scheme, args.tls_secret, args.tls_mode, args.export_ca, args.renew_certificate,
                args.encrypted, args.existing_secret, args.storage_class, args.state_dir,
                args.ingress_port, args.image_repository, args.image_tag, args.image_digest, args.listen_address)):
            raise ValueError("Cert-manager setup is a separate action; do not combine it with application installation settings")
        if not args.issuer or not HOSTNAME.fullmatch(args.issuer):
            raise ValueError("Cert-manager setup requires a valid --issuer name")
        if bool(args.email) == bool(args.issuer_file):
            raise ValueError("Cert-manager setup needs either --email for HTTP01 or --issuer-file for a reviewed namespaced Issuer")
        if args.issuer_kind not in (None, "Issuer"):
            raise ValueError("The setup helper creates only a namespaced Issuer")
        if args.issuer_file:
            source = Path(args.issuer_file).expanduser()
            if source.is_symlink() or not source.is_file() or source.stat().st_size > 262144:
                raise ValueError("Use a regular reviewed Issuer file of at most 256 KiB")
            source = source.resolve()
            if args.identity and source == Path(args.identity):
                raise ValueError("The SSH identity cannot be used as an Issuer file")
            try:
                document = json.loads(source.read_text())
            except (OSError, ValueError):
                raise ValueError("The reviewed Issuer file must contain one JSON object") from None
            if (not isinstance(document, dict) or document.get("apiVersion") != "cert-manager.io/v1"
                    or document.get("kind") != "Issuer" or "items" in document
                    or not isinstance(document.get("metadata"), dict)
                    or document["metadata"].get("name") != args.issuer
                    or document["metadata"].get("namespace") != args.namespace
                    or not isinstance(document.get("spec"), dict) or not document["spec"]):
                raise ValueError("The JSON input must be one namespaced Issuer matching --issuer and --namespace")
            args.issuer_file = str(source)
        return
    if args.install_controller or args.email or args.issuer_file or args.dry_run:
        raise ValueError("Use --setup-cert-manager for operator/Issuer bootstrap options")
    if args.access_only:
        args.local_access = True
    if args.hostname and args.local_access:
        raise ValueError("Choose --hostname for normal LAN access, or --local-access for a private localhost connection")
    if not args.hostname and not args.local_access and not args.configure_access:
        raise ValueError("Choose --hostname for normal LAN access, or --local-access for a private localhost connection")
    if args.hostname:
        if len(args.hostname) > 253 or not HOSTNAME.fullmatch(args.hostname):
            raise ValueError("Hostname must be a DNS name without scheme, port, wildcard, or path")
        try:
            ipaddress.ip_address(args.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("Ingress requires a DNS hostname rather than an IP address")
        if args.port != 8088:
            raise ValueError("--port applies only to --local-access")
        if args.scheme == "http" and args.tls_secret:
            raise ValueError("--scheme http cannot be combined with --tls-secret")
    elif args.local_access and (args.tls_secret or args.ingress_class or args.ingress_port or args.scheme):
        raise ValueError("--tls-secret, --ingress-class and --scheme require --hostname")
    elif not args.configure_access and (args.tls_secret or args.ingress_class or args.ingress_port or args.scheme):
        raise ValueError("--tls-secret, --ingress-class and --scheme require --hostname")
    if args.scheme == "http" and args.tls_secret:
        raise ValueError("--scheme http cannot be combined with --tls-secret")
    tls_options = (args.tls_mode, args.issuer, args.issuer_kind)
    if args.local_access and any(tls_options):
        raise ValueError("TLS options require Ingress, not --local-access")
    if args.scheme == "http" and any(tls_options):
        raise ValueError("TLS options cannot be combined with --scheme http")
    if args.issuer and args.tls_mode not in (None, "cert-manager"):
        raise ValueError("--issuer requires --tls-mode cert-manager")
    if args.renew_certificate and not args.configure_access:
        raise ValueError("--renew-certificate requires --configure-access")
    if args.export_ca:
        destination = Path(args.export_ca).expanduser()
        if destination.is_symlink():
            raise ValueError("CA export destination must not be a symlink")
        destination = destination.resolve()
        if destination.is_relative_to(ROOT):
            raise ValueError("Export the public CA outside the repository")
        if args.identity and destination == Path(args.identity):
            raise ValueError("CA export destination cannot be the SSH identity file")
        args.export_ca = str(destination)


def remote_command(payload: str, owner: str, args: argparse.Namespace, *, interactive: bool = True) -> str:
    sudo = ["sudo", *([] if interactive else ["-n"]), "--"]
    if args.setup_cert_manager:
        setup = ["bash", f"{payload}/scripts/setup-cert-manager.sh", "--local-k3s",
                 "--namespace", args.namespace, "--issuer", args.issuer]
        if args.email:
            setup += ["--email", args.email]
        if args.issuer_file:
            setup += ["--issuer-file", f"{payload}/issuer-input.json"]
        if args.ingress_class:
            setup += ["--ingress-class", args.ingress_class]
        for option in ("context", "kubeconfig"):
            if getattr(args, option):
                setup += ["--" + option, getattr(args, option)]
        if args.install_controller:
            setup += ["--install-controller"]
        if args.dry_run:
            setup += ["--dry-run"]
        return shlex.join([*sudo, *setup])
    installer = f"{payload}/scripts/install.sh"
    common = ["bash", installer, "--local-k3s", "--owner-user", owner,
              "--namespace", args.namespace, "--release", args.release, "--port", str(args.port)]
    if args.state_dir:
        common += ["--state-dir", args.state_dir]
    for option in ("context", "kubeconfig", "existing_secret", "storage_class", "hostname", "ingress_class", "ingress_port", "tls_secret", "scheme", "tls_mode", "issuer", "issuer_kind", "image_repository", "image_tag", "image_digest", "listen_address"):
        value = getattr(args, option, None)
        if value is not None:
            common += ["--" + option.replace("_", "-"), str(value)]
    if getattr(args, "encrypted", False):
        common += ["--encrypted"]
    if args.configure_access:
        common += ["--configure-access"]
    if args.renew_certificate:
        common += ["--renew-certificate"]
    common += ["--export-ca", str(Path(payload).parent / "public-ca.crt")]
    if args.local_access:
        common += ["--local-access"]
    if args.no_access:
        common += ["--no-access"]
    if args.access_only:
        common += ["--access-only"]
    elif args.local_access and not args.no_access:
        common += ["--access"]
    # One sudo invocation covers installation and optional private access. No
    # password, key, hash, or kubeconfig contents appear in its arguments.
    return shlex.join([*sudo, *common])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--ssh", required=True, metavar="USER@HOST", help="K3s node or existing SSH alias")
    result.add_argument("--identity", help="SSH identity filename (contents are never read by this script)")
    result.add_argument("--context", help="K3s context on the remote host (default: default)")
    result.add_argument("--kubeconfig", help="Absolute kubeconfig path on the remote K3s host; never copied locally")
    result.add_argument("--namespace", default="wealthfolio")
    result.add_argument("--release", default="wealthfolio")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--hostname", help="LAN DNS hostname for the root Ingress, without scheme or port")
    mode.add_argument("--local-access", action="store_true", help="use a private localhost connection instead of Ingress")
    result.add_argument("--ingress-class", help="existing Ingress class (K3s default: traefik)")
    result.add_argument("--ingress-port", type=int, help="externally reachable Ingress port if not the scheme default")
    result.add_argument("--tls-secret", help="existing namespace-local TLS Secret for HTTPS")
    result.add_argument("--tls-mode", choices=("controller", "secret", "cert-manager", "private-ca"), help="HTTPS certificate source; interactive installs ask when omitted")
    result.add_argument("--issuer", help="existing cert-manager issuer name")
    result.add_argument("--issuer-kind", choices=("Issuer", "ClusterIssuer"), help="cert-manager issuer scope (default: Issuer)")
    result.add_argument("--export-ca", help="local public CA certificate destination outside the repository")
    result.add_argument("--renew-certificate", action="store_true", help="explicitly renew a managed private-CA leaf during --configure-access")
    bootstrap = result.add_argument_group("explicit cert-manager setup action")
    bootstrap.add_argument("--setup-cert-manager", action="store_true", help="prepare a namespaced Issuer instead of installing the application")
    bootstrap.add_argument("--email", help="ACME account email for HTTP01 setup")
    bootstrap.add_argument("--issuer-file", help="reviewed JSON containing one namespaced Issuer; staged privately with mode 0600")
    bootstrap.add_argument("--install-controller", action="store_true", help="explicitly allow pinned cert-manager installation if missing")
    bootstrap.add_argument("--dry-run", action="store_true", help="show setup plan without installing the operator or Issuer")
    result.add_argument("--scheme", choices=("http", "https"), help="LAN URL scheme; first installs ask when omitted, a TLS Secret selects HTTPS")
    result.add_argument("--port", type=int, default=8088, help="localhost browser port, default 8088")
    result.add_argument("--state-dir", help="private absolute recovery directory on the K3s node")
    result.add_argument("--configure-access", action="store_true", help="explicitly update access settings of this managed installation")
    result.add_argument("--encrypted", action="store_true", help="require encrypted databases on this fresh installation")
    result.add_argument("--storage-class", help="StorageClass for the new retained claim")
    result.add_argument("--image-repository", help="Reviewed image repository or mirror; requires --image-digest")
    result.add_argument("--image-tag", help="Reviewed image tag (defaults to the official version)")
    result.add_argument("--image-digest", help="Reviewed image SHA256 digest; requires --image-repository")
    result.add_argument("--listen-address", choices=("0.0.0.0", "::"), help="Application listener inside the pod for a fresh install")
    result.add_argument("--existing-secret", help="reuse an existing authentication Secret")
    result.add_argument("--no-access", action="store_true", help="finish after installation without opening a tunnel")
    result.add_argument("--access-only", action="store_true", help=argparse.SUPPRESS)
    return result


def ca_destination(args: argparse.Namespace) -> Path:
    if args.export_ca:
        return Path(args.export_ca)
    target = hashlib.sha256(args.ssh.encode()).hexdigest()[:16]
    return Path.home() / ".local/share/wealthfolio/ca-certificates" / target / f"{args.namespace}-{args.release}-ca.pem"


def save_public_ca(source: Path, destination: Path, expected: dict) -> str:
    """Validate certificate-only bytes and atomically save; never install trust."""
    data = source.read_bytes()
    if len(data) > 65536 or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise ValueError("Public CA download checksum mismatch")
    text = data.decode("ascii")
    if "PRIVATE KEY" in text or text.count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("CA export contains private material or is not one certificate")
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=text)
    fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(text)).hexdigest()
    if fingerprint != expected["fingerprint"]:
        raise ValueError("Public CA fingerprint mismatch")
    destination = destination.expanduser()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError("CA export destination must not be a symlink")
    if destination.exists():
        if not stat.S_ISREG(destination.stat().st_mode) or destination.read_bytes() != data:
            raise ValueError("A different file already exists at the CA export destination; select a new --export-ca path")
        return fingerprint
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())
    return fingerprint


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        validate_args(args)
        for program in ("ssh", "scp"):
            if not shutil.which(program):
                raise ValueError(f"OpenSSH {program} is required on this workstation")
        interactive = sys.stdin.isatty()
        with tempfile.TemporaryDirectory(prefix="wf-ssh-") as temporary:
            directory = Path(temporary)
            directory.chmod(0o700)
            archive, digest = create_bundle(ROOT, directory, Path(args.issuer_file) if args.issuer_file else None)
            control = str(directory / "control")
            options = ["-o", f"ControlPath={control}", "-o", "ConnectTimeout=15",
                       "-o", "StrictHostKeyChecking=ask"]
            if not interactive:
                options += ["-o", "BatchMode=yes"]
            if args.identity:
                options += ["-i", args.identity]
            remote_directory: str | None = None
            try:
                print(f"Connecting to {args.ssh}; SSH host-key verification stays enabled.", flush=True)
                subprocess.run(["ssh", *options, "-T", "-M", "-N", "-f", "-o", "ControlPersist=120", args.ssh], check=True)
                probe = subprocess.run(["ssh", *options, args.ssh, shlex.join(["python3", "-c", PROBE_REMOTE])],
                                       check=True, capture_output=True, text=True)
                account = json.loads(probe.stdout)
                if not isinstance(account, dict) or not isinstance(account.get("user"), str) or not isinstance(account.get("directory"), str):
                    raise ValueError("Unexpected remote setup response")
                owner = account["user"]
                candidate_directory = account["directory"]
                staging = Path(candidate_directory)
                if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", owner) or not staging.is_absolute()
                        or ".." in staging.parts or not re.fullmatch(r"wealthfolio-install-[A-Za-z0-9_-]+", staging.name)):
                    raise ValueError("Unexpected remote account or staging path")
                remote_directory = candidate_directory
                subprocess.run(["scp", *options, "--", str(archive), f"{args.ssh}:{remote_directory}/setup.tar"], check=True)
                unpack = subprocess.run(["ssh", *options, args.ssh,
                                         shlex.join(["python3", "-c", EXTRACT_REMOTE, remote_directory, digest])],
                                        check=True, capture_output=True, text=True)
                payload = unpack.stdout.strip()
                if payload != f"{remote_directory}/payload":
                    raise ValueError("Unexpected remote setup result")
                session = ["ssh", *options, "-t" if interactive else "-T", "-o", "ExitOnForwardFailure=yes"]
                if args.local_access and (args.access_only or not args.no_access):
                    session += ["-L", f"127.0.0.1:{args.port}:127.0.0.1:{args.port}"]
                    print(f"Browser access: http://localhost:{args.port} once the app is ready. Keep this terminal open; Ctrl-C stops access only.", flush=True)
                subprocess.run([*session, args.ssh, remote_command(payload, owner, args, interactive=interactive)], check=True)
                public_file = f"{remote_directory}/public-ca.crt"
                ca_info = {"exists": False}
                if not args.setup_cert_manager:
                    ca_result = subprocess.run(["ssh", *options, "-o", "BatchMode=yes", args.ssh,
                                                shlex.join(["python3", "-c", PUBLIC_CA_REMOTE, public_file])],
                                               check=True, capture_output=True, text=True)
                    ca_info = json.loads(ca_result.stdout)
                if ca_info.get("exists"):
                    public_copy = directory / "public-ca.crt"
                    subprocess.run(["scp", *options, "--", f"{args.ssh}:{public_file}", str(public_copy)], check=True)
                    public_copy.chmod(0o600)
                    destination = ca_destination(args)
                    fingerprint = save_public_ca(public_copy, destination, ca_info)
                    print(f"Public CA certificate: {destination}")
                    print("SHA-256 certificate fingerprint: " + ":".join(fingerprint[i:i+2] for i in range(0, len(fingerprint), 2)))
                    print("Import this public CA into each client trust store after verifying its fingerprint. The installer did not change your trust stores.")
            finally:
                if remote_directory:
                    cleanup = ("import os,pathlib,re,shutil,sys; p=pathlib.Path(sys.argv[1]); "
                               "assert p.is_absolute() and '..' not in p.parts and re.fullmatch(r'wealthfolio-install-[A-Za-z0-9_-]+',p.name); "
                               "assert not p.is_symlink() and p.stat().st_uid==os.getuid(); shutil.rmtree(p)")
                    try:
                        result = subprocess.run(["ssh", *options, "-o", "BatchMode=yes", args.ssh, shlex.join(["python3", "-c", cleanup, remote_directory])],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                        cleanup_failed = result.returncode != 0
                    except (OSError, subprocess.TimeoutExpired):
                        cleanup_failed = True
                    if cleanup_failed:
                        print(f"Temporary setup files remain at {args.ssh}:{remote_directory}. Recovery files are stored separately.", file=sys.stderr)
                subprocess.run(["ssh", *options, "-O", "exit", args.ssh], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    except KeyboardInterrupt:
        print("\nAccess session stopped. An installed application continues running.", file=sys.stderr)
        return 130
    except (ValueError, OSError, json.JSONDecodeError) as error:
        print(f"Setup: {error}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as error:
        if error.returncode in (130, 143):
            return 130
        if error.stderr:
            # Captured stderr is from SSH or the public-source transfer probe,
            # never from the interactive credential-bearing installer session.
            print(error.stderr.strip(), file=sys.stderr)
        print(f"Setup command failed (exit {error.returncode}). Review the preceding output; recovery files are not removed.", file=sys.stderr)
        return error.returncode if 0 < error.returncode < 256 else 1


if __name__ == "__main__":
    raise SystemExit(main())
