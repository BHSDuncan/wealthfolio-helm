#!/usr/bin/env python3
"""Install Wealthfolio with a retained external Secret and a private recovery key."""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import fcntl
import getpass
import hashlib
import importlib.metadata
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.parse
import venv

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_REPOSITORY = "ghcr.io/wealthfolio/wealthfolio"
OFFICIAL_TAG = "3.11.0"
OFFICIAL_DIGEST = "sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994"
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")
LABEL = "wealthfolio.github.io/installer"


def validate_access(args, *, complete=False):
    if args.ingress_port is not None and not 1 <= args.ingress_port <= 65535:
        raise InstallError("The external Ingress port must be between 1 and 65535.")
    if args.configure_access and args.access_only:
        raise InstallError("--access-only never changes configuration and cannot be combined with --configure-access.")
    if args.access:
        args.local_access = True  # Compatibility with the original private-access flag.
    if args.hostname and args.local_access:
        raise InstallError("Choose --hostname for Ingress or --local-access for private access, not both.")
    if args.local_access and any((args.scheme, args.ingress_class, args.ingress_port, args.tls_secret, args.tls_mode, args.issuer)):
        raise InstallError("Ingress scheme, class and TLS settings require --hostname, not --local-access.")
    if not args.hostname:
        if any((args.scheme, args.ingress_class, args.ingress_port, args.tls_secret, args.tls_mode, args.issuer)) and not args.configure_access:
            raise InstallError("Ingress scheme, class and TLS settings require --hostname.")
        if not args.local_access and not args.access_only and not args.configure_access:
            raise InstallError("Select --hostname NAME for normal Ingress access or --local-access for a private tunnel.")
        return
    if len(args.hostname) > 253 or any(not NAME.fullmatch(label) or len(label) > 63 for label in args.hostname.split(".")):
        raise InstallError("Hostname must be a lowercase DNS name, without a URL scheme, port, path or wildcard.")
    try:
        ipaddress.ip_address(args.hostname)
    except ValueError:
        pass
    else:
        raise InstallError("Use a DNS hostname for Ingress, not an IP address.")
    if args.scheme == "http" and any((args.tls_secret, args.tls_mode, args.issuer)):
        raise InstallError("TLS options use HTTPS and cannot be combined with --scheme http.")
    if args.tls_mode == "controller" and args.tls_secret:
        raise InstallError("Controller-certificate mode does not use a per-application TLS Secret.")
    if args.issuer and args.tls_mode not in (None, "cert-manager"):
        raise InstallError("--issuer belongs to --tls-mode cert-manager.")
    if args.renew_certificate and (not args.configure_access or args.tls_mode != "private-ca"):
        raise InstallError("Renewal requires --configure-access --tls-mode private-ca --renew-certificate.")
    if complete and args.scheme == "https":
        if not args.tls_mode:
            raise InstallError("Choose a --tls-mode for HTTPS.")
        if args.tls_mode == "secret" and not args.tls_secret:
            raise InstallError("Existing-secret mode requires --tls-secret NAME.")
        if args.tls_mode == "cert-manager" and not args.issuer:
            raise InstallError("Cert-manager mode requires an existing --issuer NAME.")
    for name in (args.ingress_class, args.tls_secret, args.issuer):
        if name and (len(name) > 253 or any(not NAME.fullmatch(label) or len(label) > 63 for label in name.split("."))):
            raise InstallError("Ingress class and TLS Secret names must be valid Kubernetes DNS names.")


def saved_url(values, port=8088):
    ingress = values.get("ingress", {})
    if ingress.get("enabled"):
        host = ingress["hosts"][0]
        scheme = "https" if any(host in item.get("hosts", []) for item in ingress.get("tls", [])) else "http"
        for origin in values.get("app", {}).get("corsAllowOrigins", []):
            parsed = urllib.parse.urlsplit(origin)
            if parsed.scheme == scheme and parsed.hostname == host:
                return origin
        return scheme + "://" + host
    return f"http://localhost:{port}"


class InstallError(Exception):
    """An actionable failure whose message contains no credential contents."""


def run(command, *, data=None, check=True):
    """Capture diagnostics privately; never echo command input or Secret output."""
    result = subprocess.run([str(v) for v in command], input=data, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and result.returncode:
        # Operation names and fixed diagnostic categories are safe; arguments,
        # stdout, and arbitrary stderr may contain credentials or private values.
        operation = Path(str(command[0])).name
        words = [str(value) for value in command[1:3]]
        if words[:2] == ["get", "values"]:
            operation += " get values"
        elif words and words[0] in {"list", "version", "template", "install", "upgrade", "status"}:
            operation += " " + words[0]
        stderr = result.stderr or ""
        detail = "check target access and resource events"
        if re.search(r"unknown flag:\s+--all(?:\s|$)", stderr):
            detail = "unsupported option --all; check the installed Helm version"
        elif "unknown flag:" in stderr:
            detail = "unsupported command option; check the installed tool version"
        elif "permission denied" in stderr.lower() or "forbidden" in stderr.lower():
            detail = "permission denied by the target or local filesystem"
        elif "unauthorized" in stderr.lower() or "authentication required" in stderr.lower():
            detail = "target authentication failed"
        elif "connection refused" in stderr.lower() or "unable to connect" in stderr.lower():
            detail = "cannot connect to the target"
        raise InstallError(f"Command failed: {operation} (exit {result.returncode}): {detail}. "
                           "Credential-bearing command input and output were withheld.")
    return result


def helm_list_status_flags(helm):
    """Include every release status with both supported Helm CLI generations."""
    version = run([helm, "version", "--short"]).stdout.strip()
    match = re.match(r"^v?([34])\.", version)
    if not match:
        raise InstallError("The installer requires Helm 3 or Helm 4; its version could not be verified.")
    # Helm 4 removed --all because all statuses are now the default. Helm 3
    # needs it to retain the installer's failed/pending/history safety checks.
    return ["--all"] if match.group(1) == "3" else []


def secure_directory(path: Path, uid: int, gid: int):
    """Create private state without following symlinks or changing existing parents."""
    path = path.expanduser().absolute()
    try:
        path.relative_to(ROOT)
    except ValueError:
        pass
    else:
        raise InstallError("Recovery state must be outside the repository.")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            if os.geteuid() == 0:
                os.chown(current, uid, gid)
            continue
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise InstallError("Recovery state cannot contain symlinks or non-directories.")
    info = path.stat()
    if info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700:
        raise InstallError("Existing recovery directory must be owned by the selected user with mode 0700.")
    return path


def private_file(path: Path, uid: int):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o600:
        raise InstallError("Recovery files must be regular user-owned files with mode 0600.")


def write_private(path: Path, value: str, uid: int, gid: int, *, replace=False):
    if path.exists() or path.is_symlink():
        private_file(path, uid)
        if not replace:
            raise InstallError("Refusing to overwrite existing recovery material.")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        if os.geteuid() == 0:
            os.fchown(fd, uid, gid)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def locked_tool(name: str, directory: Path):
    executable = directory / name
    host = platform.system().lower() + "-" + {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    locks = json.loads((ROOT / "tools.lock.json").read_text())["platforms"]
    spec = locks.get(host, {}).get(name)
    if not spec:
        raise InstallError(f"Install {name} on this platform before running the installer.")
    marker = directory / (name + ".archive-sha256")
    if executable.is_file() and marker.is_file() and marker.read_text().strip() == spec["sha256"]:
        return str(executable)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    print(f"Downloading checksum-locked {name} {spec['version']}...")
    with urllib.request.urlopen(spec["url"], timeout=120) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != spec["sha256"]:
        raise InstallError("Downloaded tool checksum did not match the repository lock.")
    if spec["member"]:
        import io
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = archive.getmember(spec["member"])
            if not member.isfile():
                raise InstallError("Tool archive does not contain the expected executable.")
            data = archive.extractfile(member).read()
    temporary = executable.with_suffix(".new")
    with temporary.open("wb") as stream:
        stream.write(data)
    temporary.chmod(0o700)
    temporary.replace(executable)
    marker.write_text(spec["sha256"] + "\n")
    marker.chmod(0o600)
    return str(executable)


def hash_password(password: str, state: Path, *, allow_system_bootstrap=False):
    """Use the pinned Argon2 library without placing the password in argv or files."""
    versions = {"argon2-cffi": "25.1.0", "argon2-cffi-bindings": "25.1.0", "cffi": "2.0.0", "pycparser": "2.23"}
    try:
        available = all(importlib.metadata.version(k) == v for k, v in versions.items())
    except importlib.metadata.PackageNotFoundError:
        available = False
    python = sys.executable
    if not available:
        environment = state / "installer-venv"
        python = str(environment / "bin/python")
        if not Path(python).is_file():
            print("Preparing the small password-hashing dependency environment...")
            if allow_system_bootstrap and os.geteuid() == 0:
                # Ubuntu's default Python omits ensurepip until this package exists.
                print("Checking Python venv support; missing support is installed with python3-venv on Debian/Ubuntu.")
                result = subprocess.run(["bash", str(ROOT / "scripts/prepare-python.sh")])
                if result.returncode:
                    raise InstallError("Python venv support could not be prepared. No application credentials were changed.")
            try:
                venv.EnvBuilder(with_pip=True).create(environment)
            except (OSError, subprocess.CalledProcessError) as error:
                raise InstallError("Python venv support is required (Ubuntu: install python3-venv), then rerun the same command.") from error
        result = run([python, "-m", "pip", "install", "--disable-pip-version-check", "--require-hashes", "-r", ROOT / "scripts/installer-requirements.lock"], check=False)
        if result.returncode:
            raise InstallError("Password-hashing dependencies could not be installed. Check internet access and Python venv support, then rerun.")
    code = "from argon2 import PasswordHasher; import sys; print(PasswordHasher(time_cost=3,memory_cost=65536,parallelism=1).hash(sys.stdin.read()))"
    encoded = run([python, "-c", code], data=password).stdout.strip()
    if not encoded.startswith("$argon2id$v=19$m=65536,t=3,p=1$"):
        raise InstallError("Password hash generation failed validation.")
    return encoded


def resolve_kubernetes_target(args):
    """Resolve local client settings without creating state or contacting the API."""
    if args.context is not None and not args.context.strip():
        raise InstallError("The selected Kubernetes context must not be empty.")
    config_path = args.kubeconfig
    if config_path is not None:
        if not config_path or not Path(config_path).expanduser().is_file():
            raise InstallError("The selected kubeconfig file is missing or unreadable; no other target was selected.")
    elif os.environ.get("KUBECONFIG"):
        if not any(Path(name).is_file() for name in os.environ["KUBECONFIG"].split(os.pathsep) if name):
            raise InstallError("KUBECONFIG does not select an available configuration file; no other target was selected.")
    else:
        config_path = str(Path.home() / ".kube/config")
        if not Path(config_path).is_file():
            raise InstallError("No usable default kubeconfig file was found. Configure kubectl or select --kubeconfig FILE; no other target was selected.")
    binary = shutil.which("kubectl") or str(ROOT / ".tools/bin/kubectl")
    if not Path(binary).is_file():
        raise InstallError("kubectl is required on the installation machine.")
    kubectl, helm_target = [binary], []
    if config_path:
        config = str(Path(config_path).expanduser().absolute())
        kubectl += ["--kubeconfig", config]
        helm_target += ["--kubeconfig", config]
    context = args.context
    if not context:
        result = run(kubectl + ["config", "current-context"], check=False)
        context = result.stdout.removesuffix("\r\n") if result.stdout.endswith("\r\n") else result.stdout.removesuffix("\n")
        if result.returncode or not context.strip() or "\n" in context or "\r" in context:
            raise InstallError("No usable current Kubernetes context was found. Configure kubectl or select --context NAME/--kubeconfig FILE; no other target was selected.")
    kubectl += ["--context", context]
    helm_target += ["--kube-context", context]
    return kubectl, helm_target, context


class Installer:
    def __init__(self, args):
        self.args = args
        validate_access(args)
        owner_name = args.owner_user or os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
        try:
            self.owner = pwd.getpwnam(owner_name)
        except KeyError as error:
            raise InstallError("The recovery-file owner must be an existing local user.") from error
        if os.geteuid() != 0 and self.owner.pw_uid != os.getuid():
            raise InstallError("Only root can select a different recovery-file owner.")
        self.uid, self.gid = self.owner.pw_uid, self.owner.pw_gid
        self.kubectl, self.helm_target, self.context = resolve_kubernetes_target(args)
        self.namespace = args.namespace
        self.release = args.release
        self.secret = args.existing_secret or self.release + "-auth"
        self.state = None
        self.record = None

    def kube(self, *arguments, data=None, check=True, namespaced=True):
        command = self.kubectl + (["--namespace", self.namespace] if namespaced else []) + list(arguments)
        return run(command, data=data, check=check)

    def object(self, kind, name, *, namespaced=True):
        # The installer needs Secret identity only, never its existing contents.
        output = "jsonpath={.metadata}" if kind == "secret" else "json"
        result = self.kube("get", kind, name, "--ignore-not-found", "-o", output, namespaced=namespaced)
        if not result.stdout.strip():
            return None
        value = json.loads(result.stdout)
        return {"metadata": value} if kind == "secret" else value

    def persist(self):
        write_private(self.state / "state.json", json.dumps(self.record, indent=2) + "\n", self.uid, self.gid, replace=True)

    def write_private(self, path, text, *, replace=False):
        return write_private(Path(path), text, self.uid, self.gid, replace=replace)

    def secure_directory(self, path):
        return secure_directory(Path(path), self.uid, self.gid)

    def private_file(self, path):
        return private_file(Path(path), self.uid)

    def tls_options(self):
        return {"mode": self.args.tls_mode if self.args.scheme == "https" else None, "secretName": self.args.tls_secret, "issuerName": self.args.issuer, "issuerKind": self.args.issuer_kind or "Issuer"}

    def saved_tls_options(self, values):
        pending = self.record.get("pendingAccess", {})
        if pending.get("proposedValues") == values and pending.get("tlsOptions"):
            return pending["tlsOptions"]
        if self.record.get("values") == values and self.record.get("tlsOptions"):
            return self.record["tlsOptions"]
        tls = values.get("ingress", {}).get("tls", [])
        secret = next((entry.get("secretName") for entry in tls if entry.get("secretName")), None)
        return {"mode": ("secret" if secret else "controller") if tls else None, "secretName": secret, "issuerName": None, "issuerKind": "Issuer"}

    def network_values(self):
        if self.args.local_access:
            return {"auth": {"cookieSecure": "auto"}, "app": {"corsAllowOrigins": [f"http://localhost:{self.args.port}", f"http://127.0.0.1:{self.args.port}"]}, "ingress": {"enabled": False}}
        scheme = self.args.scheme or ("https" if self.args.tls_mode or self.args.tls_secret else "http")
        origin = scheme + "://" + self.args.hostname
        if self.args.ingress_port is not None:
            origin += ":" + str(self.args.ingress_port)
        ingress = {"enabled": True, "className": self.args.ingress_class if self.args.ingress_class is not None else "", "hosts": [self.args.hostname], "tls": []}
        if scheme == "https":
            tls = {"hosts": [self.args.hostname]}
            if self.args.tls_mode != "controller":
                tls["secretName"] = self.args.tls_secret
            ingress["tls"] = [tls]
        return {"auth": {"cookieSecure": "true" if scheme == "https" else "false"}, "app": {"corsAllowOrigins": [origin]}, "ingress": ingress}

    def verify_saved_access(self, values=None):
        values = self.record.get("values", {}) if values is None else values
        ingress = values.get("ingress", {})
        url = saved_url(values, self.args.port)
        if self.args.local_access and ingress.get("enabled"):
            raise InstallError("This installation uses Ingress at " + url + ". Open that URL; changing access mode requires an explicit Helm upgrade.")
        if self.args.ingress_port is not None:
            actual = urllib.parse.urlsplit(url)
            if not ingress.get("enabled") or self.args.ingress_port != (actual.port or (443 if actual.scheme == "https" else 80)):
                raise InstallError("The saved Ingress port differs. Use --configure-access to change it.")
        if self.args.hostname:
            if not ingress.get("enabled") or ingress.get("hosts") != [self.args.hostname]:
                raise InstallError("The saved hostname/access mode differs. Network changes require an explicit Helm upgrade.")
            if self.args.scheme and not url.startswith(self.args.scheme + "://"):
                raise InstallError("The saved HTTP/HTTPS scheme differs. Network changes require an explicit Helm upgrade.")
            if self.args.ingress_class is not None and ingress.get("className", "") != self.args.ingress_class:
                raise InstallError("The saved Ingress class differs. Network changes require an explicit Helm upgrade.")
            if self.args.tls_secret and ingress.get("tls") != [{"secretName": self.args.tls_secret, "hosts": [self.args.hostname]}]:
                raise InstallError("The saved TLS Secret differs. Network changes require an explicit Helm upgrade.")
            options = self.saved_tls_options(values)
            if self.args.tls_mode and self.args.tls_mode != options["mode"]:
                raise InstallError("The saved certificate mode differs. Use --configure-access to change it.")
            if self.args.issuer and self.args.issuer != options.get("issuerName"):
                raise InstallError("The saved certificate issuer differs. Use --configure-access to change it.")
            if self.args.issuer_kind and self.args.issuer_kind != options.get("issuerKind", "Issuer"):
                raise InstallError("The saved issuer kind differs. Use --configure-access to change it.")

    def hydrate_network(self, values, *, include_protocol=True):
        """Reuse recorded choices without inventing a scheme or rotating anything."""
        ingress = values.get("ingress", {})
        if not self.args.hostname and not self.args.local_access:
            if ingress.get("enabled"):
                self.args.hostname = ingress["hosts"][0]
            else:
                self.args.local_access = True
        if self.args.hostname and ingress.get("enabled"):
            if self.args.ingress_class is None:
                self.args.ingress_class = ingress.get("className", "")
            if self.args.ingress_port is None:
                self.args.ingress_port = urllib.parse.urlsplit(saved_url(values)).port
            if include_protocol:
                self.args.scheme = self.args.scheme or saved_url(values).split(":", 1)[0]
                options = self.saved_tls_options(values)
                self.args.tls_mode = self.args.tls_mode or options.get("mode")
                self.args.tls_secret = self.args.tls_secret or options.get("secretName")
                self.args.issuer = self.args.issuer or options.get("issuerName")
                self.args.issuer_kind = self.args.issuer_kind or options.get("issuerKind", "Issuer")

    def choose_network(self, *, current=None, reuse_saved=False):
        if reuse_saved and current:
            self.verify_saved_access(current)
            self.hydrate_network(current)
        if self.args.local_access:
            return
        if not self.args.scheme and any((self.args.tls_secret, self.args.tls_mode, self.args.issuer)):
            self.args.scheme = "https"
        if not self.args.scheme:
            if not sys.stdin.isatty():
                raise InstallError("Choose --scheme http or --scheme https --tls-mode MODE for a noninteractive installation; HTTP is never selected silently.")
            default = saved_url(current).split(":", 1)[0] if current else "https"
            while True:
                choice = input(f"Browser protocol: http or https [{default}]: ").strip().lower() or default
                if choice in {"http", "https"}:
                    self.args.scheme = choice
                    break
                print("Enter http or https.")
        if self.args.scheme == "https":
            self.choose_certificate(current)
        validate_access(self.args, complete=True)

    def choose_certificate(self, current):
        previous = self.saved_tls_options(current) if current else {}
        if not self.args.tls_mode:
            self.args.tls_mode = "cert-manager" if self.args.issuer else ("secret" if self.args.tls_secret else None)
        if not self.args.tls_mode:
            if not sys.stdin.isatty():
                raise InstallError("Choose --tls-mode controller, secret, cert-manager or private-ca for noninteractive HTTPS.")
            default = previous.get("mode") or "controller"
            print("HTTPS certificate methods:")
            print("  controller: use the controller certificate; it may be untrusted if no valid certificate is configured")
            print("  secret: use an existing namespace-local TLS Secret")
            print("  cert-manager: request a certificate from an existing ready Issuer or ClusterIssuer")
            print("  private-ca: generate a private CA and certificate; trust its public CA on your devices")
            while True:
                choice = input(f"Certificate method [{default}]: ").strip().lower() or default
                if choice in {"controller", "secret", "cert-manager", "private-ca"}:
                    self.args.tls_mode = choice
                    break
                print("Choose controller, secret, cert-manager or private-ca.")
        if self.args.tls_mode == "controller":
            print("Controller certificate selected. Verify that it covers your hostname and is trusted; an unconfigured controller may present an untrusted generated certificate.")
        elif self.args.tls_mode == "secret" and not self.args.tls_secret:
            previous_name = previous.get("secretName") or ""
            if not sys.stdin.isatty():
                if previous_name:
                    self.args.tls_secret = previous_name
                else:
                    raise InstallError("Existing-secret mode requires --tls-secret NAME for noninteractive HTTPS.")
            else:
                suffix = f" [{previous_name}]" if previous_name else ""
                self.args.tls_secret = input(f"Existing TLS Secret in {self.namespace}{suffix}: ").strip() or previous_name
                if not self.args.tls_secret:
                    raise InstallError("No Secret selected. Choose controller or private-ca mode when you do not already have a TLS Secret.")
        elif self.args.tls_mode == "cert-manager":
            if not self.args.issuer_kind:
                default_kind = previous.get("issuerKind", "Issuer")
                if sys.stdin.isatty() and not self.args.issuer:
                    choice = input(f"Existing issuer kind: Issuer or ClusterIssuer [{default_kind}]: ").strip() or default_kind
                    if choice not in {"Issuer", "ClusterIssuer"}:
                        raise InstallError("Issuer kind must be Issuer or ClusterIssuer.")
                    self.args.issuer_kind = choice
                else:
                    self.args.issuer_kind = default_kind
            if not self.args.issuer:
                if not sys.stdin.isatty():
                    raise InstallError("Cert-manager mode requires --issuer NAME and optionally --issuer-kind ClusterIssuer.")
                default_name = previous.get("issuerName") or ""
                self.args.issuer = input(f"Existing {self.args.issuer_kind} name [{default_name}]: ").strip() or default_name
                if not self.args.issuer:
                    raise InstallError("Choose controller/private-ca mode, or prepare an issuer with scripts/setup-cert-manager.sh before retrying cert-manager mode.")
            self.args.tls_secret = self.args.tls_secret or self.default_certificate_name("-managed-tls")
        elif self.args.tls_mode == "private-ca":
            self.args.tls_secret = self.args.tls_secret or self.default_certificate_name("-private-tls")

    def default_certificate_name(self, suffix):
        candidate = self.release + suffix
        if len(candidate) <= 63:
            return candidate
        identity = hashlib.sha256(self.release.encode()).hexdigest()[:8]
        return self.release[:63 - len(suffix) - 9].rstrip("-") + "-" + identity + suffix

    def require_tls_secret(self, namespace):
        if self.args.tls_mode == "secret" and self.args.tls_secret and (not namespace or not self.object("secret", self.args.tls_secret)):
            raise InstallError("The TLS Secret must already exist in the selected namespace; the installer does not create certificates.")

    def provision_tls(self, namespace):
        self.require_tls_secret(namespace)
        if self.args.scheme != "https" or self.args.tls_mode == "controller":
            return {}
        if self.args.tls_mode == "secret":
            return {"mode": "secret", "secretName": self.args.tls_secret}
        spec = importlib.util.spec_from_file_location("wealthfolio_tls_bootstrap", ROOT / "scripts/tls_bootstrap.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        try:
            result = module.provision(self, self.args.tls_mode, self.args.hostname, self.args.tls_secret, self.args.issuer, self.args.issuer_kind or "Issuer", renew=self.args.renew_certificate)
        except module.TLSSetupError as error:
            raise InstallError(str(error)) from error
        if result.get("secretName") != self.args.tls_secret:
            raise InstallError("Certificate provisioning returned an unexpected Secret identity.")
        return result

    def show_public_ca(self, material):
        source = material.get("publicCaFile") if material else None
        if not source:
            return
        data = Path(source).read_bytes()
        expected = material.get("publicCaSha256")
        if hashlib.sha256(data).hexdigest() != expected or b"PRIVATE KEY" in data or data.count(b"-----BEGIN CERTIFICATE-----") != 1 or not re.fullmatch(rb"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----\s*", data):
            raise InstallError("The public CA certificate did not match its saved identity; no file was exported.")
        fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(data.decode("ascii"))).hexdigest()
        print("Public CA certificate: " + str(source))
        print("Public CA file SHA256: " + expected)
        print("SHA-256 certificate fingerprint (DER): " + ":".join(fingerprint[i:i + 2] for i in range(0, len(fingerprint), 2)))
        print("Trust this public CA on each client device after verifying its fingerprint. Never share the CA private key.")
        if self.args.export_ca:
            target = Path(self.args.export_ca).expanduser().absolute()
            if not target.parent.exists():
                self.secure_directory(target.parent)
            if target.exists() or target.is_symlink():
                self.private_file(target)
                if target.read_bytes() != data:
                    raise InstallError("Refusing to overwrite a different public CA export; select a new --export-ca path.")
            else:
                self.write_private(target, data.decode("ascii"))
            print("Exported public CA certificate: " + str(target))

    def helm_values(self):
        return json.loads(run([self.helm, "get", "values", self.release, *self.helm_target, "--namespace", self.namespace, "--output", "json"]).stdout)

    def configure_access(self, release, namespace, claim_uid):
        """Explicit, fenced network-only update of an installer-managed release."""
        chart = {}
        for line in (ROOT / "charts/wealthfolio/Chart.yaml").read_text().splitlines():
            if line.startswith(("name:", "version:", "appVersion:")):
                key, value = line.split(":", 1)
                chart[key] = value.strip().strip("\"'")
        if release.get("chart") != chart.get("name", "") + "-" + chart.get("version", "") or str(release.get("app_version")) != chart.get("appVersion"):
            raise InstallError("Access configuration requires the same installed chart and application version. Use the reviewed upgrade procedure for version changes.")
        old_values = copy.deepcopy(self.record.get("values", {}))
        old_tls_options = self.saved_tls_options(old_values)
        if self.args.renew_certificate and old_tls_options.get("mode") != "private-ca":
            raise InstallError("Renewal requires an existing private-ca installation. Select private-ca without --renew-certificate to create its certificate first.")
        current_values = self.helm_values()
        pending = self.record.get("pendingAccess")
        if pending:
            if pending.get("claimUid") != claim_uid or pending.get("secretUid") != self.record["secretUid"]:
                raise InstallError("Pending access change belongs to different storage or credentials.")
            proposed = pending.get("proposedValues", {})
            if current_values not in (old_values, proposed):
                raise InstallError("Helm values changed outside the recorded access attempt. Inspect the release; no configuration was overwritten.")
            self.verify_saved_access(proposed)
            self.hydrate_network(proposed, include_protocol=False)
            self.verify_saved_access(proposed)
            self.hydrate_network(proposed)
            validate_access(self.args, complete=True)
            print("Retrying the recorded access change to " + saved_url(proposed, self.args.port))
        else:
            if current_values != old_values:
                raise InstallError("Helm values changed outside the installer. Inspect the current release and saved recovery state before configuring access.")
            self.hydrate_network(old_values, include_protocol=False)
            self.choose_network(current=old_values)
            network = self.network_values()
            proposed = copy.deepcopy(old_values)
            for key in ("auth", "app", "ingress"):
                proposed[key] = {**proposed.get(key, {}), **network[key]}
        self.require_tls_secret(namespace)
        if proposed == old_values and not pending and self.tls_options() == old_tls_options and not self.args.renew_certificate:
            print("Access settings are unchanged: " + saved_url(old_values, self.args.port))
            self.show_public_ca(self.record.get("tlsMaterial", {}))
            return
        old_url, new_url = saved_url(old_values, self.args.port), saved_url(proposed, self.args.port)
        print(f"Access change: {old_url} -> {new_url}")
        print("Certificate method: " + str(old_tls_options.get("mode") or "HTTP") + " -> " + str(self.tls_options().get("mode") or "HTTP"))
        print("The authentication Secret, master key, data claim, image and database-encryption setting are retained.")
        if not pending and sys.stdin.isatty() and input("Apply this access change? [y/N]: ").strip().lower() not in {"y", "yes"}:
            print("Access change cancelled; existing settings were retained.")
            return
        network = {"auth": {"cookieSecure": proposed["auth"]["cookieSecure"]}, "app": {"corsAllowOrigins": proposed["app"]["corsAllowOrigins"]}, "ingress": proposed["ingress"]}
        if not pending:
            material = self.provision_tls(namespace)
            self.show_public_ca(material)
            attempt = secrets.token_hex(6)
            override = self.state / ("access-change-" + attempt + ".json")
            before = self.state / ("values-before-access-" + attempt + ".json")
            write_private(override, json.dumps(network, indent=2) + "\n", self.uid, self.gid)
            write_private(before, json.dumps(old_values, indent=2) + "\n", self.uid, self.gid)
            pending = {"status": "prepared", "claimUid": claim_uid, "secretUid": self.record["secretUid"], "proposedValues": proposed, "overrideFile": override.name, "previousValuesFile": before.name, "tlsOptions": self.tls_options(), "tlsMaterial": material, "renewCertificate": self.args.renew_certificate}
            self.record["pendingAccess"] = pending
            self.persist()
        else:
            material = pending.get("tlsMaterial", {})
            if material.get("secretUid"):
                tls_secret = self.object("secret", material["secretName"])
                if not tls_secret or tls_secret["metadata"]["uid"] != material["secretUid"]:
                    raise InstallError("The provisioned TLS Secret was replaced during the pending access change.")
            self.show_public_ca(material)
            override = self.state / pending["overrideFile"]
            private_file(override, self.uid)
            if json.loads(override.read_text()) != network:
                raise InstallError("The recorded access override differs from its pending target; no upgrade was run.")
        # A lost response may have left a successfully deployed owned proposal.
        if current_values != proposed or release.get("status") != "deployed":
            result = run([self.helm, "upgrade", self.release, ROOT / "charts/wealthfolio", *self.helm_target, "--namespace", self.namespace, "--reuse-values", "--values", override, "--wait", "--timeout", "10m"], check=False)
            if result.returncode:
                pending["status"] = "failed"
                self.persist()
                raise InstallError("The access upgrade did not complete. Original credentials and last-known values remain saved. Inspect the release, then retry explicitly with --configure-access.")
        try:
            after_secret = self.object("secret", self.secret)
            after_claim = self.object("pvc", self.release)
            if not after_secret or after_secret["metadata"]["uid"] != self.record["secretUid"] or not after_claim or after_claim["metadata"]["uid"] != claim_uid:
                raise InstallError("Storage or credential identity changed during access configuration.")
            if self.helm_values() != proposed:
                raise InstallError("The deployed values differ from the recorded network-only proposal.")
            self.kube("rollout", "status", "deployment/" + self.release, "--timeout=10m")
        except (InstallError, OSError, ValueError, KeyError, TypeError):
            pending["status"] = "verification-failed"
            self.persist()
            raise InstallError("Access readback did not verify. Original values and pending target were retained; inspect the release before retrying --configure-access.")
        write_private(self.state / "values.json", json.dumps(proposed, indent=2) + "\n", self.uid, self.gid, replace=True)
        identity_source = "saved-original-uid" if self.record.get("claimUid") else "current-helm-owned-legacy-snapshot"
        self.record.update(values=proposed, claimUid=claim_uid, status="installed", tlsOptions=pending.get("tlsOptions", self.tls_options()), tlsMaterial=pending.get("tlsMaterial", {}), lastAccessChange={"from": old_url, "to": new_url, "previousValuesFile": pending["previousValuesFile"], "claimIdentitySource": identity_source, "certificateRenewed": pending.get("renewCertificate", False)})
        self.record.pop("pendingAccess", None)
        self.persist()
        print("Access configuration verified. Open " + new_url)

    def preflight(self):
        version = json.loads(self.kube("version", "-o", "json", namespaced=False).stdout)["serverVersion"]["gitVersion"]
        self.server_version = version
        match = re.match(r"v?(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (1, 34):
            raise InstallError("Kubernetes 1.34 or newer is required.")
        endpoint = self.kube("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}", namespaced=False).stdout.strip()
        if not endpoint:
            raise InstallError("The selected Kubernetes context has no API server.")
        identity = hashlib.sha256((self.context + "\n" + endpoint).encode()).hexdigest()[:16]
        parsed_endpoint = urllib.parse.urlsplit(endpoint)
        display_endpoint = urllib.parse.urlunsplit((parsed_endpoint.scheme, parsed_endpoint.netloc.rsplit("@", 1)[-1], parsed_endpoint.path, "", ""))
        print(f"Target: context {self.context}, API {display_endpoint}; Kubernetes {version}; namespace/release {self.namespace}/{self.release}.")
        default_state = Path(self.owner.pw_dir) / ".local/share/wealthfolio" / identity / self.namespace / self.release
        self.state = secure_directory(Path(self.args.state_dir) if self.args.state_dir else default_state, self.uid, self.gid)
        lock_path = self.state / ".install.lock"
        self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        if os.geteuid() == 0:
            os.fchown(self.lock_fd, self.uid, self.gid)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise InstallError("Another installer is using this recovery directory.") from error
        private_file(lock_path, self.uid)
        state_file = self.state / "state.json"
        if state_file.exists() or state_file.is_symlink():
            private_file(state_file, self.uid)
            self.record = json.loads(state_file.read_text())
            expected = {"target": identity, "namespace": self.namespace, "release": self.release, "secret": self.secret}
            if any(self.record.get(k) != v for k, v in expected.items()):
                raise InstallError("Recovery state belongs to a different target, release or Secret.")
        else:
            if self.args.access_only:
                raise InstallError("No completed installation state was found for private access.")
            self.record = {"target": identity, "namespace": self.namespace, "release": self.release, "secret": self.secret, "status": "prepared", "installationId": secrets.token_hex(16)}
        namespace = self.object("namespace", self.namespace, namespaced=False)
        if not namespace and self.record.get("namespaceUid"):
            raise InstallError("The recorded namespace is missing. Refusing to recreate it with old credentials automatically.")
        if namespace and self.record.get("namespaceUid") and namespace["metadata"]["uid"] != self.record["namespaceUid"]:
            raise InstallError("The namespace was replaced; refusing to reuse old recovery state.")
        if namespace:
            self.record["namespaceUid"] = namespace["metadata"]["uid"]
        print("Recovery directory: " + str(self.state))
        self.helm = shutil.which("helm") or (str(ROOT / ".tools/bin/helm") if (ROOT / ".tools/bin/helm").is_file() else locked_tool("helm", self.state / "tools"))
        self.helm_list_status_flags = helm_list_status_flags(self.helm)
        return namespace

    def execute(self):
        try:
            namespace = self.preflight()
            self.install(namespace)
        finally:
            if hasattr(self, "lock_fd"):
                os.close(self.lock_fd)

    def install(self, namespace):
        existing_releases = json.loads(run([self.helm, "list", *self.helm_target, "--namespace", self.namespace, *self.helm_list_status_flags, "--filter", "^" + self.release + "$", "--output", "json"]).stdout) if namespace else []
        secret = self.object("secret", self.secret) if namespace else None
        claim = self.object("pvc", self.release) if namespace else None
        previous_app = self.record.get("values", {}).get("app", {})
        if previous_app and self.args.listen_address and self.args.listen_address != previous_app.get("listenAddress", "0.0.0.0"):
            raise InstallError("The saved application listener differs. Changing it requires a reviewed Helm upgrade.")
        if existing_releases:
            if self.record.get("status") not in {"installed", "install-started"} or not self.record.get("secretUid"):
                raise InstallError("A Helm release already exists. The installer will not adopt or upgrade it; use the upgrade guide.")
            if not secret or secret["metadata"]["uid"] != self.record["secretUid"]:
                raise InstallError("The original authentication Secret is missing or replaced. Restore it before continuing.")
            if self.record.get("pendingAccess") and not self.args.configure_access:
                raise InstallError("An access change is pending verification. Retry explicitly with --configure-access; existing recovery material was retained.")
            retrying_access = self.args.configure_access and self.record.get("pendingAccess") and existing_releases[0].get("status") == "failed"
            if existing_releases[0].get("status") != "deployed" and not retrying_access:
                raise InstallError("The previous Helm install is incomplete. Its credentials and storage are retained; inspect the release before an explicit recovery or upgrade.")
            if self.record.get("status") == "install-started" and claim:
                annotations = claim["metadata"].get("annotations", {})
                if annotations.get("meta.helm.sh/release-name") != self.release or annotations.get("meta.helm.sh/release-namespace") != self.namespace:
                    raise InstallError("The pending install's claim does not have the expected Helm ownership.")
                self.kube("rollout", "status", "deployment/" + self.release, "--timeout=10m")
                self.record.update(status="installed", claimUid=claim["metadata"]["uid"])
                self.persist()
            legacy_claim = self.args.configure_access and claim and not self.record.get("claimUid") and claim.get("status", {}).get("phase") == "Bound" and claim["metadata"].get("annotations", {}).get("meta.helm.sh/release-name") == self.release and claim["metadata"].get("annotations", {}).get("meta.helm.sh/release-namespace") == self.namespace
            if not claim or (claim["metadata"]["uid"] != self.record.get("claimUid") and not legacy_claim):
                raise InstallError("The original persistent claim is missing or replaced.")
            if self.args.configure_access:
                return self.configure_access(existing_releases[0], namespace, claim["metadata"]["uid"])
            self.verify_saved_access()
            self.kube("rollout", "status", "deployment/" + self.release, "--timeout=10m")
            print("This installation already exists. Its image, storage and credentials were left unchanged.")
            return self.finish()
        if self.args.access_only:
            raise InstallError("The recorded Helm release is not installed. Private access did not create or change anything.")
        if self.args.configure_access:
            raise InstallError("--configure-access requires an existing installer-managed release; it never creates a new installation.")
        if self.record.get("status") == "installed":
            raise InstallError("This state records a prior installation, but its release is missing. Use the recovery procedure instead of creating empty replacement storage.")
        if claim:
            raise InstallError("A retained or unrelated claim already exists. Reinstall explicitly with the original Secret and existingClaim using the upgrade guide.")
        if secret and not self.args.existing_secret:
            if not self.record.get("secretUid") or secret["metadata"]["uid"] != self.record["secretUid"]:
                if secret["metadata"].get("labels", {}).get(LABEL) == self.record["installationId"] and (self.state / "master-key").is_file() and (self.state / "password-hash").is_file():
                    self.record["secretUid"] = secret["metadata"]["uid"]
                    self.persist()
                else:
                    raise InstallError("An authentication Secret already exists outside this installer state. Select it explicitly with --existing-secret NAME to reuse it.")
        if self.args.existing_secret and not secret:
            raise InstallError("The selected existing Secret was not found.")
        previous_values = self.record.get("values")
        explicit_network = any((self.args.scheme, self.args.tls_mode, self.args.tls_secret, self.args.issuer, self.args.ingress_class, self.args.ingress_port, self.args.local_access))
        fresh_network_change = bool(previous_values) and (explicit_network or self.record.get("tlsProvisioningFailed"))
        if fresh_network_change:
            if self.record.get("status") not in {"prepared", "secret-created", "install-started"} or self.object("deployment", self.release):
                raise InstallError("Existing application history/workload prevents pre-install access changes. Use the reviewed recovery or --configure-access procedure.")
            image = previous_values.get("image", {})
            for field, supplied in (("repository", self.args.image_repository), ("tag", self.args.image_tag), ("digest", self.args.image_digest)):
                if supplied is not None and supplied != image.get(field):
                    raise InstallError("A pre-install certificate retry retains the original image; use the original image options.")
            if self.args.encrypted and not previous_values.get("encryption", {}).get("required"):
                raise InstallError("A pre-install certificate retry cannot change the saved encryption setting.")
            if self.args.storage_class is not None and self.args.storage_class != previous_values.get("persistence", {}).get("storageClass"):
                raise InstallError("A pre-install certificate retry cannot change the saved storage class.")
            self.hydrate_network(previous_values, include_protocol=False)
        repository = self.args.image_repository or OFFICIAL_REPOSITORY
        digest = self.args.image_digest or OFFICIAL_DIGEST
        tag = self.args.image_tag or OFFICIAL_TAG
        if bool(self.args.image_repository) != bool(self.args.image_digest):
            raise InstallError("Custom images require both --image-repository and --image-digest.")
        self.choose_network(current=previous_values, reuse_saved=bool(previous_values) and not fresh_network_change)
        network = self.network_values()
        values = {"fullnameOverride": self.release, "auth": {"existingSecret": self.secret, **network["auth"]}, "image": {"repository": repository, "tag": tag, "digest": digest}, "app": network["app"], "ingress": network["ingress"], "encryption": {"required": self.args.encrypted}}
        if self.args.listen_address:
            values["app"]["listenAddress"] = self.args.listen_address
        self.require_tls_secret(namespace)
        if self.args.storage_class:
            values["persistence"] = {"storageClass": self.args.storage_class}
        if fresh_network_change:
            retained = copy.deepcopy(previous_values)
            for field in ("auth", "app", "ingress"):
                retained[field] = {**retained.get(field, {}), **network[field]}
            values = retained
        if previous_values and previous_values != values and not fresh_network_change:
            raise InstallError("Install settings differ from the saved attempt. Rerun the original command; changes require an explicit upgrade.")
        values_path = self.state / "values.json"
        self.record["values"] = values
        self.record["tlsOptions"] = self.tls_options()
        self.persist()
        write_private(values_path, json.dumps(values, indent=2) + "\n", self.uid, self.gid, replace=values_path.exists())
        # All schema/template failures happen before credentials or namespace writes.
        run([self.helm, "template", self.release, ROOT / "charts/wealthfolio", "--kube-version", self.server_version, "--namespace", self.namespace, "--values", values_path])
        if not namespace:
            document = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": self.namespace}}
            self.kube("create", "-f", "-", data=json.dumps(document), namespaced=False)
            namespace = self.object("namespace", self.namespace, namespaced=False)
            self.record["namespaceUid"] = namespace["metadata"]["uid"]
            self.persist()
        try:
            self.record["tlsMaterial"] = self.provision_tls(namespace)
        except (InstallError, OSError, ValueError):
            self.record["tlsProvisioningFailed"] = True
            self.persist()
            raise
        self.record.pop("tlsProvisioningFailed", None)
        self.persist()
        self.show_public_ca(self.record["tlsMaterial"])
        if not secret:
            key_file, hash_file = self.state / "master-key", self.state / "password-hash"
            if hash_file.exists():
                if not key_file.exists():
                    raise InstallError("Incomplete recovery material. Preserve it and inspect the private state before retrying.")
                private_file(key_file, self.uid)
                private_file(hash_file, self.uid)
                key, password_hash = key_file.read_text().strip(), hash_file.read_text().strip()
            else:
                if not sys.stdin.isatty():
                    raise InstallError("Use an interactive terminal for the first password, or select an existing Secret.")
                password = getpass.getpass("New Wealthfolio password (at least 12 characters): ")
                confirmation = getpass.getpass("Confirm password: ")
                if len(password) < 12 or password != confirmation:
                    raise InstallError("Passwords must match and contain at least 12 characters. Rerun to try again.")
                password_hash = hash_password(password, self.state, allow_system_bootstrap=os.geteuid() == 0)
                del password, confirmation
                if key_file.exists():
                    private_file(key_file, self.uid)
                    key = key_file.read_text().strip()
                else:
                    key = base64.b64encode(secrets.token_bytes(32)).decode()
                    write_private(key_file, key + "\n", self.uid, self.gid)
                write_private(hash_file, password_hash + "\n", self.uid, self.gid)
            if len(base64.b64decode(key, validate=True)) != 32 or not password_hash.startswith("$argon2id$"):
                raise InstallError("Saved recovery material failed validation; no Secret was created.")
            self.persist()
            payload = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": self.secret, "namespace": self.namespace, "labels": {LABEL: self.record["installationId"]}}, "type": "Opaque", "stringData": {"WF_SECRET_KEY": key, "WF_AUTH_PASSWORD_HASH": password_hash}}
            self.kube("create", "-f", "-", data=json.dumps(payload))
            secret = self.object("secret", self.secret)
        self.record["secretUid"] = secret["metadata"]["uid"]
        self.record["status"] = "secret-created"
        self.persist()
        print("Installing Wealthfolio; startup can take several minutes...")
        self.record["status"] = "install-started"
        self.persist()
        result = run([self.helm, "install", self.release, ROOT / "charts/wealthfolio", *self.helm_target, "--namespace", self.namespace, "--values", values_path, "--wait", "--timeout", "10m"], check=False)
        if result.returncode:
            raise InstallError("Helm did not complete. Credentials and storage were preserved. Inspect pods/events on the printed target; rerunning never upgrades or replaces an existing release.")
        claim = self.object("pvc", self.release)
        if not claim or claim.get("status", {}).get("phase") != "Bound":
            raise InstallError("Installation returned without a bound claim. Recovery material was preserved.")
        self.record.update(status="installed", claimUid=claim["metadata"]["uid"])
        self.persist()
        self.finish()

    def finish(self):
        print("Wealthfolio is installed.")
        if (self.state / "master-key").is_file():
            print("Keep its master-key file independently backed up; it is required to recover encrypted data.")
        else:
            print("The selected external Secret was reused. Keep its original master key independently backed up.")
        print("Database encryption: " + ("enabled" if self.record.get("values", {}).get("encryption", {}).get("required") else "off (credential vault remains encrypted)"))
        self.show_public_ca(self.record.get("tlsMaterial", {}))
        if self.record.get("values", {}).get("ingress", {}).get("enabled"):
            print("Open " + saved_url(self.record["values"]))
            print("The hostname must resolve to your Ingress controller. No SSH session is needed to use the app.")
            return
        origins = self.record.get("values", {}).get("app", {}).get("corsAllowOrigins", [])
        if f"http://localhost:{self.args.port}" not in origins:
            raise InstallError("This local port differs from the saved browser origin. Reuse the original --port setting.")
        print(f"Open http://localhost:{self.args.port} while this private access command is running:")
        prefix = self.kubectl + ["--namespace", self.namespace, "port-forward", "service/" + self.release, f"{self.args.port}:8088", "--address", "127.0.0.1"]
        import shlex
        print("  " + shlex.join(prefix))
        if not self.args.no_access:
            print("Starting private access. Press Ctrl-C to stop access; the application stays installed.")
            try:
                subprocess.run(prefix, check=True)
            except KeyboardInterrupt:
                pass
            except subprocess.CalledProcessError as error:
                raise InstallError("The application remains installed, but private access could not start. Close any existing listener on the selected port and rerun the access helper.") from error


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--context", help="Override the local kubeconfig's current context")
    result.add_argument("--kubeconfig", help="Select a kubeconfig file instead of KUBECONFIG or ~/.kube/config")
    result.add_argument("--namespace", default="wealthfolio")
    result.add_argument("--release", default="wealthfolio")
    result.add_argument("--owner-user", help="Local owner of private recovery files; defaults to sudo's original user")
    result.add_argument("--state-dir", help="Private recovery directory outside this repository")
    result.add_argument("--existing-secret", help="Reuse an explicitly selected pre-existing external Secret")
    result.add_argument("--image-repository")
    result.add_argument("--image-tag")
    result.add_argument("--image-digest")
    result.add_argument("--listen-address", choices=("0.0.0.0", "::"), help="Application listener inside the pod for a fresh install")
    result.add_argument("--storage-class")
    result.add_argument("--encrypted", action="store_true", help="Require encrypted profile databases on this fresh installation")
    result.add_argument("--hostname", help="DNS hostname for normal root-path Ingress access")
    result.add_argument("--scheme", choices=("http", "https"), help="Ingress URL scheme; first-time interactive installs ask when omitted")
    result.add_argument("--ingress-class", help="Existing Ingress class; omitted uses the cluster default")
    result.add_argument("--ingress-port", type=int, help="Externally reachable Ingress port when it differs from the scheme default")
    result.add_argument("--tls-mode", choices=("controller", "secret", "cert-manager", "private-ca"), help="HTTPS certificate source; interactive setup offers these choices")
    result.add_argument("--tls-secret", help="Existing TLS Secret in secret mode, or optional output name for issued certificates")
    result.add_argument("--issuer", help="Existing cert-manager Issuer/ClusterIssuer; selects cert-manager mode")
    result.add_argument("--issuer-kind", choices=("Issuer", "ClusterIssuer"), help="Kind of existing cert-manager issuer (default: Issuer)")
    result.add_argument("--export-ca", help="Copy only the generated public CA certificate to this file when available")
    result.add_argument("--renew-certificate", action="store_true", help="With explicit private-ca access configuration, renew its leaf certificate while retaining its CA")
    result.add_argument("--local-access", action="store_true", help="Use private localhost forwarding instead of an Ingress hostname")
    result.add_argument("--configure-access", action="store_true", help="Explicitly review/change only network settings on a managed installation")
    result.add_argument("--no-access", action="store_true", help="With --local-access, finish without starting port-forwarding")
    result.add_argument("--access", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--access-only", action="store_true", help="Open private access to a recorded installation; never install")
    result.add_argument("--port", type=int, default=8088)
    result.add_argument("--yes", action="store_true", help=argparse.SUPPRESS)
    return result


def main(argv=None):
    if sys.version_info < (3, 10):
        raise InstallError("Python 3.10 or newer is required.")
    args = parser().parse_args(argv)
    for name in (args.namespace, args.release, args.existing_secret):
        if name and (len(name) > 53 or not NAME.fullmatch(name)):
            raise InstallError("Namespace, release and Secret names must be lowercase DNS labels of at most 53 characters.")
    if not 1 <= args.port <= 65535:
        raise InstallError("The local port must be between 1 and 65535.")
    os.umask(0o077)
    Installer(args).execute()


if __name__ == "__main__":
    try:
        main()
    except (InstallError, OSError, ValueError, KeyError, TypeError) as error:
        print("Install stopped: " + (str(error) if isinstance(error, InstallError) else "A local file, dependency or target response could not be used safely."), file=sys.stderr)
        sys.exit(1)
