#!/usr/bin/env python3
"""Explicit certificate provisioning; never installs controllers or device trust."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile

OWNER = "wealthfolio.github.io/installer"
MODE = "wealthfolio.github.io/tls-mode"
FINGERPRINT = "wealthfolio.github.io/tls-certificate-sha256"
SPEC_HASH = "wealthfolio.github.io/tls-spec-sha256"


class TLSSetupError(Exception):
    """Safe, actionable certificate setup error without private key contents."""


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _name(value):
    if not value or len(value) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", p) or len(p) > 63 for p in value.split(".")):
        raise TLSSetupError("Certificate host/resource names must be valid lowercase DNS names.")


def _read(installer, path):
    installer.private_file(path)
    return path.read_text()


def _json(installer, path):
    return json.loads(_read(installer, path)) if path.exists() or path.is_symlink() else None


def _save(installer, path, value):
    installer.write_private(path, json.dumps(value, indent=2) + "\n", replace=path.exists() or path.is_symlink())


def _openssl(*args):
    binary = shutil.which("openssl")
    if not binary:
        raise TLSSetupError("Private-CA mode needs OpenSSL on the K3s host. Install your OS openssl package, then rerun; no certificate was generated.")
    result = subprocess.run([binary, *map(str, args)], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise TLSSetupError("OpenSSL certificate generation or verification failed; private command output was withheld.")
    return result.stdout.strip()


def _owned(secret, owner, mode):
    metadata = secret.get("metadata", {})
    if metadata.get("labels", {}).get(OWNER) != owner or metadata.get("labels", {}).get(MODE) != mode:
        raise TLSSetupError("The TLS Secret already exists outside this certificate setup. Select existing-Secret mode or choose a different TLS Secret name; it was not changed.")


def _public_result(secret_name, metadata, ca=None):
    return {"secretName": secret_name, "secretUid": metadata["uid"],
            "publicCaFile": str(ca) if ca else None,
            "publicCaSha256": _sha(ca.read_bytes()) if ca else None}


def _root_ca(installer, folder, owner):
    index = folder / "ca-state.json"
    saved = _json(installer, index)
    if saved:
        if saved.get("owner") != owner:
            raise TLSSetupError("Private CA state belongs to another installation.")
        key, cert = folder / saved["key"], folder / saved["certificate"]
        for file, field in ((key, "keySha256"), (cert, "certificateSha256")):
            if _sha(_read(installer, file).encode()) != saved[field]:
                raise TLSSetupError("Private CA recovery files changed. Restore the original protected CA files before issuing another leaf certificate.")
        _openssl("verify", "-x509_strict", "-CAfile", cert, cert)
        _openssl("x509", "-in", cert, "-checkend", "86400", "-noout")
        return key, cert
    version = installer.secure_directory(folder / ("root-" + secrets.token_hex(6)))
    with tempfile.TemporaryDirectory(prefix="wf-ca-") as temporary:
        work = Path(temporary)
        config = work / "ca.cnf"
        config.write_text("[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ca\n[dn]\nCN=Wealthfolio installer private CA\n[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n")
        _openssl("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", "3650", "-config", config, "-keyout", work / "key.pem", "-out", work / "ca.pem")
        key, cert = version / "ca-key.pem", version / "ca-certificate.pem"
        installer.write_private(key, (work / "key.pem").read_text())
        installer.write_private(cert, (work / "ca.pem").read_text())
    _save(installer, index, {"owner": owner, "key": str(key.relative_to(folder)), "certificate": str(cert.relative_to(folder)), "keySha256": _sha(key.read_bytes()), "certificateSha256": _sha(cert.read_bytes())})
    return key, cert


def _leaf(installer, folder, hostname, ca_key, ca_cert):
    version = installer.secure_directory(folder / ("leaf-" + secrets.token_hex(6)))
    with tempfile.TemporaryDirectory(prefix="wf-leaf-") as temporary:
        work = Path(temporary)
        config = work / "leaf.cnf"
        config.write_text("[req]\nprompt=no\ndistinguished_name=dn\n[dn]\nCN=Wealthfolio private service\n[server]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\nsubjectAltName=DNS:" + hostname + "\n")
        _openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-config", config, "-keyout", work / "key.pem", "-out", work / "request.pem")
        _openssl("x509", "-req", "-in", work / "request.pem", "-CA", ca_cert, "-CAkey", ca_key, "-set_serial", str(secrets.randbits(120) + 1), "-sha256", "-days", "365", "-extfile", config, "-extensions", "server", "-out", work / "leaf.pem")
        _openssl("verify", "-x509_strict", "-purpose", "sslserver", "-verify_hostname", hostname, "-CAfile", ca_cert, work / "leaf.pem")
        key, cert = version / "tls-key.pem", version / "tls-certificate.pem"
        installer.write_private(key, (work / "key.pem").read_text())
        installer.write_private(cert, (work / "leaf.pem").read_text() + ca_cert.read_text())
    return {"key": str(key.relative_to(folder)), "certificate": str(cert.relative_to(folder)), "keySha256": _sha(key.read_bytes()), "certificateSha256": _sha(cert.read_bytes())}


def _private_ca(installer, hostname, secret_name, owner, *, renew):
    folder = installer.secure_directory(installer.state / "tls" / "private-ca")
    identity = {"owner": owner, "namespace": installer.namespace, "hostname": hostname, "secretName": secret_name, "mode": "private-ca"}
    index = folder / ("secret-" + _sha(secret_name.encode())[:16] + ".json")
    saved = _json(installer, index) or {**identity}
    if any(saved.get(k) != v for k, v in identity.items()):
        raise TLSSetupError("This private certificate name is bound to another hostname or installation. Choose a new TLS Secret name.")
    existing = installer.object("secret", secret_name)
    if existing:
        _owned(existing, owner, "private-ca")
        if not saved.get("active") and not saved.get("pending"):
            raise TLSSetupError("An owned TLS Secret exists but its protected certificate state is missing. Restore that state or choose a fresh Secret name; no key was read from Kubernetes.")
    if existing and saved.get("secretUid") and existing["metadata"]["uid"] != saved["secretUid"]:
        raise TLSSetupError("The managed TLS Secret was replaced; refusing to rotate a different identity.")
    ca_key, ca_cert = _root_ca(installer, folder, owner)
    pending = saved.get("pending")
    if not pending and saved.get("active"):
        active = saved["active"]
        if existing and existing["metadata"]["uid"] != saved.get("secretUid"):
            raise TLSSetupError("The managed TLS Secret was replaced; refusing to rotate a different identity.")
        for field, sha_field in (("key", "keySha256"), ("certificate", "certificateSha256")):
            if _sha(_read(installer, folder / active[field]).encode()) != active[sha_field]:
                raise TLSSetupError("Protected leaf certificate files changed; restore them before reusing or renewing this certificate.")
        if existing and existing["metadata"].get("annotations", {}).get(FINGERPRINT) != active["certificateSha256"]:
            raise TLSSetupError("TLS Secret fingerprint differs from its protected certificate state.")
        if not renew and existing:
            try:
                _openssl("x509", "-in", folder / active["certificate"], "-checkend", "604800", "-noout")
            except TLSSetupError as error:
                raise TLSSetupError("The private leaf certificate expires within seven days or is expired. Run --configure-access --tls-mode private-ca --renew-certificate to renew its leaf with the same CA and Secret identity.") from error
            return _private_result(installer, secret_name, existing, folder, active, ca_cert)
        pending = _leaf(installer, folder, hostname, ca_key, ca_cert) if renew else active
    if not pending:
        pending = _leaf(installer, folder, hostname, ca_key, ca_cert)
    saved["pending"] = pending
    _save(installer, index, saved)
    key, cert = folder / pending["key"], folder / pending["certificate"]
    for file, sha_field in ((key, "keySha256"), (cert, "certificateSha256")):
        if _sha(_read(installer, file).encode()) != pending[sha_field]:
            raise TLSSetupError("Pending private certificate files changed; no Kubernetes key was modified.")
    fingerprint = pending["certificateSha256"]
    if existing and existing["metadata"].get("annotations", {}).get(FINGERPRINT) != fingerprint:
        if not saved.get("active") or existing["metadata"].get("annotations", {}).get(FINGERPRINT) != saved["active"]["certificateSha256"]:
            raise TLSSetupError("TLS Secret fingerprint is neither the old nor pending owned certificate.")
        patch = [{"op": "test", "path": "/metadata/uid", "value": saved["secretUid"]},
                 {"op": "test", "path": "/metadata/annotations/" + FINGERPRINT.replace("/", "~1"), "value": saved["active"]["certificateSha256"]},
                 {"op": "replace", "path": "/data/tls.crt", "value": base64.b64encode(cert.read_bytes()).decode()},
                 {"op": "replace", "path": "/data/tls.key", "value": base64.b64encode(key.read_bytes()).decode()},
                 {"op": "replace", "path": "/metadata/annotations/" + FINGERPRINT.replace("/", "~1"), "value": fingerprint}]
        patch_file = folder / (".renew-patch-" + secrets.token_hex(8) + ".json")
        installer.write_private(patch_file, json.dumps(patch))
        try:
            installer.kube("patch", "secret", secret_name, "--type=json", "--patch-file", str(patch_file))
        finally:
            patch_file.unlink()
    elif not existing:
        document = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": secret_name, "namespace": installer.namespace, "labels": {OWNER: owner, MODE: "private-ca"}, "annotations": {FINGERPRINT: fingerprint}}, "type": "kubernetes.io/tls", "stringData": {"tls.crt": cert.read_text(), "tls.key": key.read_text()}}
        installer.kube("create", "-f", "-", data=json.dumps(document))
    after = installer.object("secret", secret_name)
    if not after:
        raise TLSSetupError("TLS Secret creation did not become visible. Protected files were retained for retry.")
    _owned(after, owner, "private-ca")
    if after["metadata"].get("annotations", {}).get(FINGERPRINT) != fingerprint or (existing and after["metadata"]["uid"] != existing["metadata"]["uid"]):
        raise TLSSetupError("TLS Secret identity/fingerprint readback failed. Protected files were retained.")
    saved.update(active=pending, secretUid=after["metadata"]["uid"])
    saved.pop("pending", None)
    _save(installer, index, saved)
    return _private_result(installer, secret_name, after, folder, pending, ca_cert)


def _private_result(installer, secret_name, secret, folder, active, ca_cert):
    expires = _openssl("x509", "-in", folder / active["certificate"], "-noout", "-enddate").removeprefix("notAfter=")
    print("Private CA certificate: " + str(ca_cert))
    print("Public CA file SHA-256 (PEM bytes; not the DER certificate fingerprint): " + _sha(ca_cert.read_bytes()))
    print("No device trust was changed. Leaf expiry: " + expires + ". Renew explicitly with --configure-access --tls-mode private-ca --renew-certificate; no renewal daemon is installed.")
    return {**_public_result(secret_name, secret["metadata"], ca_cert), "mode": "private-ca", "certificateExpiresAt": expires}


def _contains(actual, expected):
    """Allow API defaults while binding every requested field."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _contains(actual[k], v) for k, v in expected.items())
    return actual == expected


def _managed(installer, hostname, secret_name, owner, issuer_name, issuer_kind):
    if issuer_kind not in {"Issuer", "ClusterIssuer"} or not issuer_name:
        raise TLSSetupError("Choose an existing cert-manager Issuer or ClusterIssuer name.")
    _name(issuer_name)
    try:
        issuer = installer.object("issuer.cert-manager.io" if issuer_kind == "Issuer" else "clusterissuer.cert-manager.io", issuer_name, namespaced=issuer_kind == "Issuer")
    except Exception as error:
        raise TLSSetupError("cert-manager or issuer access is unavailable. Use scripts/setup-cert-manager.sh --help for explicit setup; no operator is installed automatically.") from error
    if not issuer or not any(c.get("type") == "Ready" and c.get("status") == "True" for c in issuer.get("status", {}).get("conditions", [])):
        raise TLSSetupError("The chosen cert-manager issuer is absent or not Ready. Configure its CA/ACME/DNS credentials first; scripts/setup-cert-manager.sh --help provides the HTTP01 bootstrap path.")
    folder = installer.secure_directory(installer.state / "tls" / "cert-manager")
    index = folder / (_sha(secret_name.encode())[:16] + ".json")
    spec = {"secretName": secret_name, "dnsNames": [hostname], "issuerRef": {"name": issuer_name, "kind": issuer_kind, "group": "cert-manager.io"}, "privateKey": {"algorithm": "RSA", "size": 2048, "rotationPolicy": "Always"}, "secretTemplate": {"labels": {OWNER: owner, MODE: "cert-manager"}}}
    digest = _sha(json.dumps(spec, sort_keys=True).encode())
    spec["secretTemplate"]["annotations"] = {SPEC_HASH: digest}
    identity = {"owner": owner, "namespace": installer.namespace, "hostname": hostname, "secretName": secret_name, "specSha256": digest}
    saved = _json(installer, index)
    if saved and any(saved.get(k) != v for k, v in identity.items()):
        raise TLSSetupError("This managed certificate name belongs to another request. Choose a different TLS Secret name.")
    certificate = installer.object("certificate.cert-manager.io", secret_name)
    secret = installer.object("secret", secret_name)
    if secret:
        _owned(secret, owner, "cert-manager")
        if secret["metadata"].get("annotations", {}).get(SPEC_HASH) != digest:
            raise TLSSetupError("Existing managed TLS Secret does not match this certificate request.")
    if certificate:
        if certificate["metadata"].get("labels", {}).get(OWNER) != owner or not _contains(certificate.get("spec"), spec) or (saved and saved.get("certificateUid") and certificate["metadata"]["uid"] != saved["certificateUid"]):
            raise TLSSetupError("Existing Certificate is unowned, replaced or has a different specification; it was not changed.")
    else:
        if secret:
            raise TLSSetupError("A TLS Secret already exists without this owned Certificate. Select existing-Secret mode instead.")
        _save(installer, index, identity)
        installer.kube("create", "-f", "-", data=json.dumps({"apiVersion": "cert-manager.io/v1", "kind": "Certificate", "metadata": {"name": secret_name, "namespace": installer.namespace, "labels": {OWNER: owner}}, "spec": spec}))
        certificate = installer.object("certificate.cert-manager.io", secret_name)
    saved = {**identity, "certificateUid": certificate["metadata"]["uid"]}
    _save(installer, index, saved)
    try:
        installer.kube("wait", "--for=condition=Ready", "certificate.cert-manager.io/" + secret_name, "--timeout=300s")
    except Exception as error:
        raise TLSSetupError("Certificate is not Ready after the bounded wait. Inspect its issuer/challenge events; existing application access was not switched. Retry after fixing DNS/CA/provider prerequisites.") from error
    latest = installer.object("certificate.cert-manager.io", secret_name)
    generation = latest.get("metadata", {}).get("generation") if latest else None
    if not latest or latest["metadata"]["uid"] != saved["certificateUid"] or not _contains(latest.get("spec"), spec) or not isinstance(generation, int) or not any(c.get("type") == "Ready" and c.get("status") == "True" and c.get("observedGeneration") == generation for c in latest.get("status", {}).get("conditions", [])):
        raise TLSSetupError("Certificate readiness does not match the current owned request generation; access was not switched.")
    after = installer.object("secret", secret_name)
    if not after:
        raise TLSSetupError("Certificate reported Ready without its TLS Secret; access was not switched.")
    _owned(after, owner, "cert-manager")
    annotations = after["metadata"].get("annotations", {})
    if annotations.get(SPEC_HASH) != digest or annotations.get("cert-manager.io/certificate-name") != secret_name or annotations.get("cert-manager.io/issuer-name") != issuer_name or annotations.get("cert-manager.io/issuer-kind") != issuer_kind:
        raise TLSSetupError("Issued TLS Secret does not bind the expected Certificate, issuer and owned request.")
    _save(installer, index, {**saved, "secretUid": after["metadata"]["uid"]})
    print("cert-manager issued the certificate. It manages renewal while this Certificate and its issuer remain configured; browser trust depends on that issuer's CA.")
    return {**_public_result(secret_name, after["metadata"]), "mode": "cert-manager", "certificateUid": saved["certificateUid"]}


def provision(installer, mode, hostname, secret_name, issuer_name=None, issuer_kind="Issuer", *, renew=False):
    """Provision only the explicit selected path, returning public metadata."""
    _name(hostname)
    if mode == "controller":
        print("Using the controller's existing/default certificate. It may be self-signed or not match this hostname; HTTPS does not automatically establish browser trust.")
        return {"secretName": None, "publicCaFile": None, "publicCaSha256": None, "mode": mode}
    _name(secret_name)
    if mode == "secret":
        secret = installer.object("secret", secret_name)
        if not secret:
            raise TLSSetupError("The selected TLS Secret does not exist in this namespace.")
        return {**_public_result(secret_name, secret["metadata"]), "mode": mode}
    owner = installer.record.get("installationId")
    if not owner:
        raise TLSSetupError("Certificate generation requires this installation's private ownership record; no existing Secret was adopted.")
    if mode == "private-ca":
        return _private_ca(installer, hostname, secret_name, owner, renew=renew)
    if mode == "cert-manager" and not renew:
        return _managed(installer, hostname, secret_name, owner, issuer_name, issuer_kind)
    raise TLSSetupError("Unknown certificate mode or unsupported renewal request; --renew-certificate is for private-ca mode only.")
