# Installation

Use standard Helm for a new installation. The chart consumes an existing authentication Secret and nonsecret values; it does not prepare hosts, create credentials or provision a cluster. The [optional guided installer](#optional-guided-installer) offers interactive setup for homelabs and first-time Kubernetes operators.

## Helm prerequisites

- Helm 3 or 4 and kubectl, with a kubeconfig you trust and can read for an existing Kubernetes 1.34+ cluster. Verify the selected context before making changes.
- Permission to create a namespace (or an existing namespace), Secret, Deployment, Service and PVC, plus Ingress only if enabled.
- Durable filesystem storage writable by UID/GID 1000. A default StorageClass serves the default 5Gi claim; otherwise choose `persistence.storageClass` or a prepared `persistence.existingClaim`.
- Linux AMD64/ARM64 cluster nodes able to pull the pinned official image from GHCR. A container build tool is unnecessary.

Ingress, TLS and cert-manager are optional. Python is needed only for the credential-generation example below or the guided installer, not for Helm. OS packages, Python environments, SSH, Kubernetes provisioning, DNS and shared controllers are outside the chart.

Chart **0.1.0** is publicly available from **`oci://ghcr.io/bhsduncan/charts/wealthfolio`**, without registry authentication. Clone the repository for the standalone credential procedure, examples and optional tooling; Helm downloads the version-pinned chart separately:

```bash
git clone https://github.com/BHSDuncan/wealthfolio-helm.git
cd wealthfolio-helm
kubectl config get-contexts
export WF_CONTEXT=your-context
```

Replace `your-context` with the verified context. These examples use namespace/release `wealthfolio`; keep the context and namespace consistent across kubectl and Helm. Set `KUBECONFIG` first if using a nondefault kubeconfig. See the [release guide](releases.md) for versioned GHCR distribution.

## Prepare credentials

The chart requires a namespace-local Secret named by `auth.existingSecret`. Its default entry names are:

| Secret key | Required application value |
|---|---|
| `WF_SECRET_KEY` | Base64 text that decodes to exactly 32 random bytes. Generate once and retain the original independently of Kubernetes. |
| `WF_AUTH_PASSWORD_HASH` | Argon2id PHC password hash with literal single `$` characters. |

An existing secret manager may supply this contract directly. Do not place passwords, hashes, master keys or PATs in Helm values, command arguments, annotations or source. The chart only renders Secret references, so credentials stay outside Helm release history. Kubernetes Secret `data` adds another base64 layer; `kubectl --from-file` below handles that layer. Base64 is not encryption: protect cluster Secret access and encryption at rest.

To prepare credentials without the installer, use Python 3.10+ with venv support and access to PyPI. Run the following from the source checkout in an interactive terminal. It creates a private directory outside the repository, installs checksum-locked hashing dependencies into an isolated environment, and prompts for the password without echoing it. `mkdir` fails if the directory already exists; use a different path for a separate installation and preserve existing recovery files.

```bash
umask 077
export WF_CREDENTIAL_DIR="$HOME/.local/share/wealthfolio-credentials"
mkdir -p "$HOME/.local/share"
mkdir -m 700 "$WF_CREDENTIAL_DIR"
python3 -m venv "$WF_CREDENTIAL_DIR/venv"
"$WF_CREDENTIAL_DIR/venv/bin/python" -m pip install --require-hashes \
  -r scripts/installer-requirements.lock
"$WF_CREDENTIAL_DIR/venv/bin/python" - <<'PYTHON'
import base64
import getpass
import os
from pathlib import Path
import secrets
from argon2 import PasswordHasher

folder = Path(os.environ["WF_CREDENTIAL_DIR"])
if any((folder / name).exists() for name in ("master-key", "password-hash")):
    raise SystemExit("Credential files already exist; preserve the original key.")
password = getpass.getpass("New Wealthfolio password (at least 12 characters): ")
confirmation = getpass.getpass("Confirm password: ")
if len(password) < 12 or password != confirmation:
    raise SystemExit("Passwords must match and contain at least 12 characters.")
password_hash = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1).hash(password)
del password, confirmation
master_key = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
for name, value in (("master-key", master_key), ("password-hash", password_hash)):
    with (folder / name).open("x") as stream:
        stream.write(value)
print("Credential files created privately; back up the original master-key separately.")
PYTHON
```

Stop if any command fails. If venv support is missing, install your operating system's Python venv package and retry the environment setup; the chart does not install OS packages. Keep the original `master-key` file in an independent protected backup. This key encrypts the vault even with database encryption disabled; losing or replacing it makes encrypted data unrecoverable. Treat the hash file as sensitive too. The login password itself is not saved.

Create the namespace and Secret using the files, so neither credential is a shell argument and `$` characters remain literal:

```bash
kubectl --context "$WF_CONTEXT" create namespace wealthfolio
kubectl --context "$WF_CONTEXT" --namespace wealthfolio create secret generic wealthfolio-auth \
  --from-file=WF_SECRET_KEY="$WF_CREDENTIAL_DIR/master-key" \
  --from-file=WF_AUTH_PASSWORD_HASH="$WF_CREDENTIAL_DIR/password-hash"
```

If your operator already created the namespace, skip its creation command. Secret creation deliberately refuses to overwrite an existing Secret. Confirm the Secret name and key names privately; do not print its data. These commands do not run the guided installer and do not create a Helm release.

## Minimum values and Helm install

Save this nonsecret file as `values.yaml`:

```yaml
auth:
  existingSecret: wealthfolio-auth
```

`auth.existingSecret` is the only required value. The Secret and both keys must already exist in the release namespace; Helm cannot validate their contents. The defaults create a retained 5Gi PVC and a private ClusterIP Service suitable for loopback forwarding.

Shell environment variables do **not** automatically become Helm values. `WF_CONTEXT` above is substituted by your shell into an explicit command option; `WF_CREDENTIAL_DIR` selects local credential files. Exporting application variables such as `WF_SECRET_KEY` does not configure Helm or create the required Secret. Helm reads defaults and the values files/explicit overrides you supply. Never pass credential contents through Helm overrides.

The preview below downloads the chart and renders locally without contacting your cluster. It selects Kubernetes 1.34 explicitly so it does not depend on Helm's built-in simulated version. You can preview your actual supported cluster version instead; installation validates the real cluster.

```bash
helm template wealthfolio oci://ghcr.io/bhsduncan/charts/wealthfolio \
  --version 0.1.0 --namespace wealthfolio \
  --kube-version 1.34.0 --values values.yaml
helm install wealthfolio oci://ghcr.io/bhsduncan/charts/wealthfolio \
  --version 0.1.0 --kube-context "$WF_CONTEXT" --namespace wealthfolio \
  --values values.yaml --wait --timeout 10m
kubectl --context "$WF_CONTEXT" --namespace wealthfolio \
  port-forward service/wealthfolio 8088:8088 --address 127.0.0.1
```

Open **http://localhost:8088** while forwarding runs. Ctrl-C closes forwarding and leaves the application installed; rerun the kubectl command to reconnect. The default CORS origins cover `http://localhost:8088` and `http://127.0.0.1:8088`. If you change the local port, update `app.corsAllowOrigins` with the exact browser origin through your values file and `helm upgrade`.

A local source installation remains supported: replace the OCI reference with `./charts/wealthfolio` from the repository root and omit `--version`. Check local chart configuration with `helm lint ./charts/wealthfolio --strict --values values.yaml`. A verified local chart archive also works in place of the OCI reference.

Sign in and confirm that saved records can be read before storing valuable data. The chart runs one replica and uses Recreate updates, stopping the old pod before starting its replacement. Upgrades therefore cause downtime. Stop every writer before maintenance or recovery. See [backup and recovery](backup-restore.md), [upgrades](upgrading.md), [security](security.md), and [compatibility](compatibility.md).

## Optional ingress and TLS

No hostname, Ingress or TLS is required for loopback access. For a hostname, arrange an existing ingress controller and DNS, then copy and edit the [HTTPS Ingress example](../examples/ingress-tls.yaml). It contains:

```yaml
auth:
  existingSecret: wealthfolio-auth
  cookieSecure: "true"
app:
  corsAllowOrigins:
    - https://wealthfolio.example.test
ingress:
  enabled: true
  className: traefik
  hosts:
    - wealthfolio.example.test
  tls:
    - secretName: wealthfolio-tls
      hosts:
        - wealthfolio.example.test
```

Replace the hostname and class with your actual DNS name and existing IngressClass. Prepare `wealthfolio-tls` as a namespace-local `kubernetes.io/tls` Secret through your certificate workflow; it must cover that hostname and be trusted by browsers. Save the edited file as `access.yaml`, then pass `--values values.yaml --values access.yaml` to `helm install` (or `helm upgrade` for later configuration). A controller-managed default certificate can instead use the [controller TLS example](../examples/ingress-controller-tls.yaml).

Use the exact browser scheme, host and external port in `app.corsAllowOrigins`, and `auth.cookieSecure: "true"` for HTTPS. For an intentional HTTP ingress, omit `ingress.tls`, use the exact `http://` origin and `auth.cookieSecure: "false"`; use TLS on networks you do not fully trust. Preserve Host and trusted forwarded protocol at the proxy. Root `/` routing is required; subpath hosting is unsupported.

**Cert-manager is optional and separate.** The chart does not install controllers, CRDs, issuers or Certificate resources. You can use your existing certificate workflow. If you choose cert-manager, manage its operator separately, configure an issuer, and request a certificate into the Secret referenced above. [HTTPS setup](https.md) covers the repository's separate bootstrap helper and guided certificate modes.

Storage selection, fresh database encryption, MCP, IPv6, Service exposure and scheduling are optional values documented in the [chart reference](../charts/wealthfolio/README.md) and [examples](../examples/). Set `encryption.required: true` before creating databases if desired; the default is false, and changing the flag does not convert existing files. Keep the same original master key in either case.

## Optional guided installer

The guided installer is a convenience for homelab users and first-time Kubernetes operators. Standard Helm installation above is the primary supported workflow. The installer runs outside the chart; host package setup, Python environments, SSH orchestration and certificate bootstrapping are never Helm hooks.

### Prerequisites and first installation

Use a macOS or Linux machine with Python 3.10+, kubectl and a trusted kubeconfig for a Kubernetes 1.34+ cluster. The cluster needs durable default storage, permission to create the application resources, and access to GHCR. AMD64 and ARM64 hosts have checksum-locked Helm downloads when Helm is absent. The installation host also needs HTTPS access to `get.helm.sh` for that download and to PyPI for the password-hashing dependencies. The installer uses the official image; Docker and local image builds are not needed.

Choose the browser hostname and point its DNS record to your existing ingress controller. From the repository checkout, run:

```bash
./scripts/install.sh --hostname wealthfolio.example.lan
```

Replace the example hostname with your browser hostname, without a scheme or path. Local execution is the default; no local-mode flag is required. The installer uses your kubeconfig's current context and prints that context, API endpoint and namespace/release before making changes. A current context can refer to a cluster on this machine or elsewhere; select the intended one with kubectl or override it with `--context NAME`.

Provide a readable kubeconfig through `--kubeconfig PATH`, `KUBECONFIG` or `~/.kube/config`; both kubectl and Helm use it. A vendor wrapper’s private configuration alone is insufficient if Helm cannot access that configuration. Missing or invalid configuration stops installation with a setup error; configure cluster access before retrying.

Namespace and release default to `wealthfolio`. Ingress and new storage use the cluster's configured defaults; select existing resources with `--ingress-class NAME` or `--storage-class NAME` when needed.

On a first interactive install, the installer asks whether to use HTTP or HTTPS. If you choose HTTPS, it offers the controller default, an existing Secret, cert-manager, or a generated private CA. It then prepares Helm and the namespace, generates a stable master key, and asks for a login password. A missing prerequisite stops the selected path; the installer does not silently fall back to HTTP. When explicitly run as root on Ubuntu/Debian, it can install missing `python3-venv` support without upgrading the operating system. Otherwise, prepare the required Python environment before retrying. Protected recovery material stays in the installing user's home directory; the printed location includes the original master key. Keep an independent copy. Credentials do not enter command arguments or Helm values.

When ready, open the printed hostname URL. Installation exits normally and you can close the terminal. DNS, the ingress controller and network access to its HTTP/HTTPS ports must already work. Only an explicitly selected certificate-provisioning mode creates certificate resources.

The chart uses standard Kubernetes resources and cluster-managed storage. Verify support for its UID/GID 1000 pod and filesystem PVC; see [compatibility](compatibility.md) for requirements and tested coverage.

### Optional installation over SSH

Use a macOS or Linux workstation with Python 3.10+ and OpenSSH. The remote Linux host needs Python 3.10+, kubectl, a readable kubeconfig and the same cluster/network prerequisites as a local installation:

```bash
./scripts/install.sh --ssh user@host --hostname wealthfolio.example.lan
```

Replace the SSH target and browser hostname. Add `--identity ~/.ssh/my-key` if needed. Commands use the SSH account’s kubeconfig/current context by default. Optional `--sudo` explicitly runs as root and uses root’s kubeconfig unless `--kubeconfig` selects another file. `--context` selects a context within the effective kubeconfig. A remote kubeconfig path must be absolute on that host; its contents are never copied to your workstation. Recovery files remain owned by the SSH account on the remote host. Hostname-based browser access continues after the installer disconnects.

### Installation options

| Option | Setting |
|---|---|
| `--context NAME` | Optional override for the current kubeconfig context |
| `--kubeconfig PATH` | Optional readable kubeconfig file; uses its current context unless overridden |
| `--hostname NAME` | Browser hostname, without scheme or path; configure its DNS record separately |
| `--scheme http` or `--scheme https` | Explicit scheme; omitted choices are asked on a first interactive install |
| `--tls-mode MODE` | `controller`, `secret`, `cert-manager`, or `private-ca`; see [HTTPS setup](https.md) |
| `--tls-secret NAME` | Existing TLS Secret, or an explicit owned output name for a provisioning mode |
| `--ingress-class NAME` | Existing ingress class; omitted uses the cluster default |
| `--storage-class NAME` | Existing StorageClass; omitted uses the cluster default |
| `--ingress-port NUMBER` | External browser port, such as `8443`; omitted uses the scheme's standard port |
| `--namespace NAME` / `--release NAME` | Installation identity; both default to `wealthfolio` |

HTTPS configures the exact HTTPS origin and Secure cookies. Controller-default HTTPS can use a self-signed or mismatched certificate; it does not establish browser trust automatically. Private-CA mode generates certificates with manual trust/export and explicit leaf renewal; cert-manager mode uses a selected issuer. Follow [HTTPS setup](https.md) for the actual prerequisites and executable bootstrap helpers. Device trust and global controllers are never changed by a normal install.

If your controller or reverse proxy serves HTTPS on a custom external port, add `--scheme https --tls-mode MODE --ingress-port 8443`. The installer records and prints the exact origin, such as `https://wealthfolio.example.lan:8443`; the TLS certificate still covers the hostname alone. Configure the controller, load balancer and firewall forwarding separately; this option does not change their listeners. HTTP01 certificate validation still requires public port 80, regardless of the browser port. The application requires a root `/` route; URL subpaths are unsupported.

For unattended operation, provide the scheme, certificate mode and its required inputs explicitly. A fresh unattended installation also needs an existing authentication Secret selected with `--existing-secret NAME` and noninteractive access to the selected cluster. SSH installation needs noninteractive SSH, and noninteractive sudo only if `--sudo` is selected. Missing required choices fail clearly; they never select HTTP implicitly.

An installed release keeps its saved scheme on ordinary reruns. Run `./scripts/install.sh --configure-access` using the same kubeconfig/current context to choose HTTPS and its certificate path. If the original installation used explicit target options or SSH, keep those options and add `--configure-access`. The saved hostname is reused when omitted; review and confirm the proposed URL change. This explicit network-only update preserves credentials, image, storage and encryption settings; see [upgrades](upgrading.md).

### Optional private localhost access

If Ingress is unavailable, choose private loopback access instead of a hostname:

```bash
./scripts/install.sh --local-access
```

Open **http://localhost:8088** in a browser on the machine running this command and keep it running. If it runs on a headless server, use a kubeconfig from your browser machine or the optional SSH tunnel below. Ctrl-C closes the private connection while leaving the application installed. Reconnect later with the same kubeconfig/current context:

```bash
./scripts/access.sh
```

Keep the same target options when reconnecting. For optional SSH tunneling, use `./scripts/install.sh --ssh user@host --local-access` and reconnect with `./scripts/access.sh --ssh user@host`; the browser then runs on the SSH workstation.

The access helper reconnects an installation originally configured for local access. It does not silently change an Ingress installation's origins or cookie policy. `--hostname` and `--local-access` are mutually exclusive; switching access settings requires explicit installer access reconfiguration. Use the same target, namespace, release, state-directory and port options when reconnecting. `--port` selects another local port if needed.

### Network and storage customization

Private local access always binds loopback; `--port NUMBER` changes its browser/forwarding port without exposing it on your LAN. This is separate from `--ingress-port`. For optional SSH installation through a nondefault port or bastion, use a host alias; the installer and file transfer honor OpenSSH's `Port`, `ProxyJump` and `IdentityFile` settings. See [OpenSSH configuration](https://man.openbsd.org/ssh_config).

Use `--state-dir /path/to/private-state` to select recovery storage outside the checkout; it must belong to the installing user with mode `0700` and contain no symlink components. In SSH mode it names a remote directory. Temporary staging honors `TMPDIR` in each host's installation process. `--storage-class NAME` selects cluster-managed persistent storage; host filesystem locations are configured through your storage provisioner, not embedded in this repository. `/data` and `/tmp` are intentional paths inside the container. For existing claims or advanced scheduling, use the Helm values reference.

For an IPv6-only pod network, select `--listen-address ::` on the fresh installation; the default is `0.0.0.0`. This selects the pod's listener while keeping its application port `8088`; cluster IP families, DNS and ingress routing remain cluster configuration. Changing a saved listener requires a reviewed Helm upgrade.

For a reviewed registry mirror, both SSH and local installers accept `--image-repository REGISTRY/IMAGE --image-tag VERSION --image-digest sha256:DIGEST`. Use the actual mirrored image digest and configure node registry access separately. Existing installations retain their saved image; updates use the upgrade procedure. Direct Helm users can also configure `imagePullSecrets`.

For proxy-based networks, configure the selected kubeconfig's [cluster proxy settings](https://kubernetes.io/docs/reference/config-api/kubeconfig.v1/#Cluster) and the installation host's proxy/trust settings. Python downloads honor standard [proxy environment variables](https://docs.python.org/3/library/urllib.request.html#urllib.request.ProxyHandler); sudo may filter them, and SSH does not copy your workstation's proxy credentials. Configure image pulls on the cluster nodes separately. A disconnected installation needs preinstalled tooling, available hashing dependencies or an existing authentication Secret, and locally reachable reviewed images; the installer does not provision an offline mirror.


### Host changes and saved files

The installer writes recovery state under `~/.local/share/wealthfolio/<target-identity>/<namespace>/<release>` for the installation owner, or the explicit `--state-dir`. Directories are mode `0700`, recovery files mode `0600`; state must stay outside the repository and cannot contain symlink components. Keep the printed directory and an independent private copy of its original master key.

Saved files include `state.json` (target/resource identities and installer status), `values.json` (nonsecret Helm configuration), `master-key` and `password-hash` when generated, and an install lock. Access changes retain before/proposed JSON files for safe retry. Optional `tools/` holds a checksum-locked Helm download, `installer-venv/` holds pinned hashing dependencies, and selected certificate modes add private TLS keys/certificates under `tls/`. An existing authentication Secret is referenced without copying its contents into state. SSH mode saves these files on the remote host, not your workstation; a requested CA export copies only the public certificate.

When Helm is missing, setup downloads it into private state rather than installing a system package. Password hashing can download locked Python packages into the private environment. Explicit root execution on Debian/Ubuntu may run `apt-get update` and install missing `python3-venv`; no OS upgrade is requested. Other users must prepare missing venv support themselves. Private-CA mode requires OpenSSL and writes owned TLS resources; cert-manager mode creates a namespaced Certificate using your selected issuer. No normal install provisions Kubernetes, changes DNS, installs an ingress controller/cert-manager, or modifies device trust. The separate `setup-cert-manager.sh --install-controller` command is an explicit optional global operation.

### Configuration while using the guided installer

Choose storage class, image overrides, pod listener and database-encryption policy at initial setup. Ordinary reruns use the recorded target and settings and do not upgrade the application. Keep the original context/kubeconfig, SSH account, namespace/release and state-directory options on subsequent commands.

Use `scripts/install.sh --configure-access` for supported URL, scheme, ingress and certificate changes, and its `--renew-certificate` flow to renew an installer-generated private-CA leaf certificate. Reconnect a local-access installation with `scripts/access.sh` and the same target options. Retry interrupted setup or access changes through the installer so its saved resource identities and pending state stay consistent.

Do not edit `state.json` or `values.json`, change release values outside the installer, replace its Secret/key/claim, or bypass its ownership and drift checks. The installer refuses releases it does not manage and releases that have changed unexpectedly. It does not provide general chart or application upgrades; those require a separately reviewed maintenance operation following [upgrades](upgrading.md), with backups and the original key.

Run `./scripts/install.sh --help` for local options, or `./scripts/install.sh --ssh user@host --help` for SSH options. Help does not contact a cluster.
