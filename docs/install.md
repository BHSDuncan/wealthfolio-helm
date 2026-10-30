# Installation

## Install locally

Use a macOS or Linux machine with Python 3.10+, kubectl and a trusted kubeconfig for a Kubernetes 1.34+ cluster. The cluster needs durable default storage, permission to create the application resources, and access to GHCR. An existing K3s server can use its bundled tools and server kubeconfig instead. AMD64 and ARM64 hosts have checksum-locked Helm downloads when Helm is absent. The installation host also needs HTTPS access to `get.helm.sh` for that download and to PyPI for the password-hashing dependencies. The installer uses the official image; Docker and local image builds are not needed.

Choose the browser hostname and point its DNS record to your existing ingress controller. From the repository checkout, run:

```bash
./scripts/install.sh --hostname wealthfolio.example.lan
```

Replace the example hostname with your browser hostname, without a scheme or path. Local execution is the default; no local-mode flag is required. The installer uses your kubeconfig's current context and prints that context, API endpoint and namespace/release before making changes. A current context can refer to a cluster on this machine or elsewhere; select the intended one with kubectl or override it with `--context NAME`.

Without a user kubeconfig, the installer detects an existing local K3s server configuration. This on-host K3s flow requires sudo: run `sudo ./scripts/install.sh --hostname wealthfolio.example.lan`. It never invokes sudo automatically. If the selected `KUBECONFIG`/`--kubeconfig` source or an existing `~/.kube/config` cannot supply a usable context, installation stops instead of switching to K3s.

Namespace and release default to `wealthfolio`. Ingress uses the cluster's default class, or `traefik` for detected/explicit local K3s; `--namespace`, `--release`, and `--ingress-class` override them.

On a first interactive install, the installer asks whether to use HTTP or HTTPS. If you choose HTTPS, it offers the controller default, an existing Secret, cert-manager, or a generated private CA. It then prepares Helm and the namespace, generates a stable master key, and asks for a login password. A missing prerequisite stops the selected path; the installer does not silently fall back to HTTP. On Ubuntu/Debian it can install `python3-venv` when missing, without upgrading the operating system. Protected recovery material stays in the installing user's home directory; the printed location includes the original master key. Keep an independent copy. Credentials do not enter command arguments or Helm values.

When ready, open the printed hostname URL. Installation exits normally and you can close the terminal. DNS, the ingress controller and network access to its HTTP/HTTPS ports must already work. Only an explicitly selected certificate-provisioning mode creates certificate resources.

## Kubernetes distributions and explicit targets

The chart and local installer also support Kubernetes distributions other than K3s. They use standard Kubernetes resources and cluster-managed storage; hostname access requires an existing ingress controller. Some distributions impose additional security policies, so verify support for the chart's UID/GID 1000 pod and filesystem PVC. See [compatibility](compatibility.md) for requirements and tested coverage.

To override the current kubeconfig context and ingress class explicitly:

```bash
./scripts/install.sh --context YOUR_CONTEXT --hostname YOUR_HOSTNAME --ingress-class YOUR_CLASS
```

Replace the context, hostname and existing ingress class. Add `--kubeconfig /path/to/config` when needed; this option also works by itself and selects that file's current context. For private forwarding, omit `--hostname` and `--ingress-class` and use `--local-access`; the browser and forwarding command run on this machine.

## Optional: installation over SSH

To manage a K3s server from another machine, use a macOS or Linux workstation with Python 3.10+ and OpenSSH. The K3s server needs Python 3.10+, durable storage, image/dependency access, SSH and sudo access:

```bash
./scripts/install.sh --ssh user@host --hostname wealthfolio.example.lan
```

Replace the SSH target and browser hostname. Add `--identity ~/.ssh/my-key` if needed. Recovery material stays on the K3s server. Hostname-based browser access continues after the installer disconnects. The access, certificate and storage options below apply to both local and SSH installation.

## Ingress options

| Option | Setting |
|---|---|
| `--hostname NAME` | Browser hostname, without scheme or path; configure its DNS record separately |
| `--scheme http` or `--scheme https` | Explicit scheme; omitted choices are asked on a first interactive install |
| `--tls-mode MODE` | `controller`, `secret`, `cert-manager`, or `private-ca`; see [HTTPS setup](https.md) |
| `--tls-secret NAME` | Existing TLS Secret, or an explicit owned output name for a provisioning mode |
| `--ingress-class NAME` | Existing ingress class; defaults to Traefik for detected/explicit local K3s, otherwise the cluster default |
| `--ingress-port NUMBER` | External browser port, such as `8443`; omitted uses the scheme's standard port |
| `--namespace NAME` / `--release NAME` | Installation identity; both default to `wealthfolio` |

HTTPS configures the exact HTTPS origin and Secure cookies. Controller-default HTTPS can use a self-signed or mismatched certificate; it does not establish browser trust automatically. Private-CA mode generates certificates with manual trust/export and explicit leaf renewal; cert-manager mode uses a selected issuer. Follow [HTTPS setup](https.md) for the actual prerequisites and executable bootstrap helpers. Device trust and global controllers are never changed by a normal install.

For an existing controller or reverse proxy serving HTTPS on a custom external port, add `--scheme https --tls-mode MODE --ingress-port 8443`. The installer records and prints the exact origin, such as `https://wealthfolio.example.lan:8443`; the TLS certificate still covers the hostname alone. This option does not change the controller, load balancer, firewall or Service listener: configure external forwarding separately. HTTP01 certificate validation still requires public port 80, regardless of the browser port. The application requires a root `/` route; URL subpaths are unsupported.

For unattended operation, provide the scheme, certificate mode and its required inputs explicitly. A fresh unattended installation also needs an existing authentication Secret selected with `--existing-secret NAME` and noninteractive access to the selected cluster. On-host K3s needs noninteractive sudo; SSH installation also needs noninteractive SSH. Missing required choices fail clearly; they never select HTTP implicitly.

An installed release keeps its saved scheme on ordinary reruns. Run `./scripts/install.sh --configure-access` using the same kubeconfig/current context to choose HTTPS and its certificate path; use sudo for the detected on-host K3s flow. If the original installation used explicit target options or SSH, keep those options and add `--configure-access`. The saved hostname is reused when omitted; review and confirm the proposed URL change. This explicit network-only update preserves credentials, image, storage and encryption settings; see [upgrades](upgrading.md).

## Optional private localhost access

If Ingress is unavailable, choose private loopback access instead of a hostname:

```bash
./scripts/install.sh --local-access
```

Open **http://localhost:8088** in a browser on the machine running this command and keep it running. If it runs on a headless server, use a kubeconfig from your browser machine or the optional SSH tunnel below. Ctrl-C closes the private connection while leaving the application installed. Reconnect later with the same kubeconfig/current context:

```bash
./scripts/access.sh
```

Use sudo for both commands for the detected on-host K3s flow. Explicit context selection remains available with `./scripts/install.sh --context YOUR_CONTEXT --local-access` and `./scripts/access.sh --context YOUR_CONTEXT`. For optional SSH tunneling to K3s, use `./scripts/install.sh --ssh user@host --local-access` and reconnect with `./scripts/access.sh --ssh user@host`; the browser then runs on the SSH workstation.

The access helper reconnects an installation originally configured for local access. It does not silently change an Ingress installation's origins or cookie policy. `--hostname` and `--local-access` are mutually exclusive; switching an existing installation requires an explicit access reconfiguration or Helm upgrade. Use the same target, namespace, release, state-directory and port options when reconnecting. `--port` selects another local port if needed.

## Advanced target overrides and help

Without target overrides, the installer follows kubectl's kubeconfig loading rules: `KUBECONFIG` selects and merges its listed files, otherwise it uses `~/.kube/config`. The selected current context is pinned for every Kubernetes/Helm operation. Only when neither source is configured does it try the standard local K3s configuration. It does not change your kubeconfig or current context.

Use `--context NAME` to override the current context or `--kubeconfig /path/to/config` to select a single file. The cert-manager setup helper uses the same defaults and optional overrides.

`--local-k3s` is an optional advanced override that forces this server's K3s configuration instead of a configured user kubeconfig. It needs sudo and defaults to context `default` and `/etc/rancher/k3s/k3s.yaml`. Normal installation and automatic K3s detection do not need this flag.

SSH installation selects the remote server's K3s configuration. In SSH mode a supplied kubeconfig path must be absolute **on the remote host**; its contents are never copied to your workstation. Keep the same target, namespace/release and recovery directory on reruns.

Run `./scripts/install.sh --help` for local K3s and explicit-context options, or `./scripts/install.sh --ssh user@host --help` for optional SSH options. These help commands do not connect to a cluster. Managed reruns preserve credentials and data and refuse to change an existing installation's settings implicitly. Do not start a second writer on the same claim.

## Network and storage customization

Private local access always binds loopback; `--port NUMBER` changes its browser/forwarding port without exposing it on your LAN. This is separate from `--ingress-port`. For optional SSH installation through a nondefault port or bastion, use a host alias; the installer and file transfer honor OpenSSH's `Port`, `ProxyJump` and `IdentityFile` settings. See [OpenSSH configuration](https://man.openbsd.org/ssh_config).

Use `--state-dir /path/to/private-state` to select recovery storage outside the checkout; it must belong to the installing user with mode `0700` and contain no symlink components. In SSH mode it names a remote directory. Temporary staging honors `TMPDIR` in each host's installation process. `--storage-class NAME` selects cluster-managed persistent storage; host filesystem locations are configured through your storage provisioner, not embedded in this repository. `/data` and `/tmp` are intentional paths inside the container. For existing claims or advanced scheduling, use the Helm values reference.

For an IPv6-only pod network, select `--listen-address ::` on the fresh installation; the default is `0.0.0.0`. This selects the pod's listener while keeping its application port `8088`; cluster IP families, DNS and ingress routing remain cluster configuration. Changing a saved listener requires a reviewed Helm upgrade.

For a reviewed registry mirror, both SSH and local installers accept `--image-repository REGISTRY/IMAGE --image-tag VERSION --image-digest sha256:DIGEST`. Use the actual mirrored image digest and configure node registry access separately. Existing installations retain their saved image; updates use the upgrade procedure. Direct Helm users can also configure `imagePullSecrets`.

For proxy-based networks, configure the selected kubeconfig's [cluster proxy settings](https://kubernetes.io/docs/reference/config-api/kubeconfig.v1/#Cluster) and the installation host's proxy/trust settings. Python downloads honor standard [proxy environment variables](https://docs.python.org/3/library/urllib.request.html#urllib.request.ProxyHandler); sudo may filter them, and SSH does not copy your workstation's proxy credentials. Configure image pulls on the cluster nodes separately. A disconnected installation needs preinstalled tooling, available hashing dependencies or an existing authentication Secret, and locally reachable reviewed images; the installer does not provision an offline mirror.

## Helm-only installation

If you already manage Secrets, create a namespace-local `wealthfolio-auth` Secret containing `WF_SECRET_KEY` and `WF_AUTH_PASSWORD_HASH`, then install directly:

```bash
helm install wealthfolio ./charts/wealthfolio \
  --kube-context my-cluster --namespace wealthfolio \
  --set auth.existingSecret=wealthfolio-auth --wait --timeout 10m
```

The master key must decode from base64 to 32 bytes. The password hash must be a literal Argon2id PHC string with single `$` characters. Keep these values out of Helm values and command arguments. The interactive installer generates them when you do not already have a secret-management workflow.

Use the [values reference](../charts/wealthfolio/README.md) and [examples](../examples/) for Ingress values, storage, fresh database encryption, MCP, and image overrides. Direct Helm installation creates a private ClusterIP Service by default; choose Ingress values or an intentional port-forward workflow. The chart uses one replica and Recreate updates, so updates cause downtime, and retains the complete data claim on uninstall. Changing the database-encryption flag does not convert existing databases.

Confirm login and persistent record readback before storing valuable data. See [backup and restore](backup-restore.md), [security](security.md), and [troubleshooting](troubleshooting.md).
