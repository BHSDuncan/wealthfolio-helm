# Wealthfolio Helm chart

Unofficial community Helm chart for [Wealthfolio](https://wealthfolio.app/). Deploys one non-root instance with persistent storage, password authentication, and optional HTTPS Ingress and MCP access. Kubernetes **1.34+**; chart **0.1.0**, application **3.11.0**.

Chart 0.1.0 is unreleased; no public chart registry is configured. Before public exposure, review the pinned image’s [recorded security limitations](docs/security.md#upstream-image).

## Install

Clone this repository on the machine where you will run the installer:

```bash
git clone https://github.com/BHSDuncan/wealthfolio-helm.git
cd wealthfolio-helm
```

Alternatively, download and extract it using GitHub’s **Code → Download ZIP**.

Use Python 3.10+ and kubectl configured for an existing Kubernetes **1.34+** cluster with durable default storage. For hostname-based browser access, use an existing ingress controller and point your chosen hostname’s DNS record to its address. Then run:

```bash
./scripts/install.sh --hostname wealthfolio.example.lan
```

**Local execution is the default; no mode flag is needed.** The installer uses the current context from `KUBECONFIG` or your default kubeconfig, which can point to a local or remote cluster. It prints the selected context and API server before making changes. Select another target with `--context NAME` or `--kubeconfig PATH`; use `--ingress-class NAME` when there is no suitable default ingress class.

On a K3s server without a user kubeconfig, the installer can use its bundled kubectl and standard local configuration. Run the same command with `sudo` for this on-host K3s flow; normal kubeconfig-based installation does not require sudo. An invalid configured context stops installation instead of falling back to another cluster.

Replace the example browser hostname. The installer prepares Helm, creates the namespace and credentials, and installs the official Wealthfolio image. Choose HTTP or HTTPS, choose a certificate path for HTTPS, and enter a login password. Keep the printed recovery-directory location. When installation finishes, open the printed application URL; the terminal can be closed.

HTTPS choices include the controller certificate, an existing TLS Secret, cert-manager, or a generated private CA. See [certificate setup](docs/https.md) for trust and certificate prerequisites. DNS and ingress controllers remain operator-managed. For an existing managed installation, add `--configure-access` to review its access settings while keeping credentials and data; retain the original target and recovery options on reruns.

The installation machine needs access to Helm/Python package downloads when dependencies are missing; cluster nodes need access to the application image. See [installation options](docs/install.md) for prerequisites, target selection and private `--local-access` forwarding without a hostname or Ingress.

## Optional: SSH installation on K3s

To run the installer from another macOS or Linux machine, use Python 3.10+ and OpenSSH on that machine, then run:

```bash
./scripts/install.sh --ssh user@host --hostname wealthfolio.example.lan
```

Replace the SSH target and browser hostname. The remote K3s server needs Python 3.10+, sudo access, durable storage and the same cluster/network prerequisites; recovery material stays on that server. SSH is only needed during setup for this hostname-based installation. See [SSH installation](docs/install.md#optional-installation-over-ssh) for keys and other options.

## Data and configuration

The chart retains the complete `/data` claim when uninstalled. **Back up the original master key separately**; losing it makes encrypted data unrecoverable. Database encryption is off by default, while the credential vault remains encrypted. Choose database encryption before creating data; changing the flag does not convert existing databases. One replica and Recreate updates support the single-writer workload; updates cause downtime. Stop all writers before maintenance or recovery.

- [Values reference](charts/wealthfolio/README.md) and [examples](examples/)
- [Backup and restore](docs/backup-restore.md) · [Upgrades and reinstall](docs/upgrading.md)
- [MCP](docs/mcp.md) · [Troubleshooting](docs/troubleshooting.md)
- [Security](docs/security.md) · [Compatibility](docs/compatibility.md)
- [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

The chart uses the official versioned Wealthfolio image pinned by digest. It does not rebuild or modify the application image. Independently authored chart and tooling files are Apache-2.0; Wealthfolio remains AGPL-3.0. See [NOTICE](NOTICE) and [image security](docs/security.md). Wealthfolio is a trademark of Teymz Inc.; no endorsement is implied.
