# Wealthfolio Helm chart

Unofficial community Helm chart for [Wealthfolio](https://wealthfolio.app/). Deploys one non-root instance with persistent storage, password authentication, and optional HTTPS Ingress and MCP access. Kubernetes **1.34+**; chart **0.1.0**, application **3.11.0**.

Chart 0.1.0 is unreleased; no public chart registry is configured. Before public exposure, review the pinned image’s [recorded security limitations](docs/security.md#upstream-image).

## Install on K3s

Download and extract this repository using GitHub’s **Code → Download ZIP**, or clone it using the URL shown under **Code**. Open a terminal in the extracted/cloned project directory.

Choose a hostname and point its DNS record to your K3s ingress address. From this checkout, run:

```bash
./scripts/install.sh --ssh user@host --hostname wealthfolio.example.lan
```

Replace the SSH target and browser hostname. The installer uses the existing Traefik ingress controller, prepares Helm, creates the namespace and credentials, and installs the official Wealthfolio image. On a first install, choose HTTP or HTTPS, choose a certificate path for HTTPS, and enter a login password. Keep the printed recovery-directory location. It prints the application URL and exits; access works without a terminal or SSH tunnel staying open.

The installer asks about HTTP/HTTPS when the choice is not supplied. HTTPS choices are the existing/default Traefik certificate, an existing TLS Secret, cert-manager, or an installer-generated private CA. Use `--scheme` and `--tls-mode` to preselect them. See [certificate setup](docs/https.md) for browser trust, managed public certificates, and private-LAN setup. DNS and the ingress controller remain operator-managed.

For an existing managed HTTP installation, rerun with `--configure-access` to choose HTTPS while keeping its credentials and data. Ordinary reruns preserve the saved configuration. See [changing access](docs/upgrading.md).

Use a macOS or Linux workstation with Python 3.10+ and OpenSSH. The K3s host needs Python 3.10+, sudo access, durable storage, and access to GHCR. See [installation options](docs/install.md) for prerequisites, SSH keys, local K3s, other clusters, and the optional `--local-access` private connection.

## Data and configuration

The chart retains the complete `/data` claim when uninstalled. **Back up the original master key separately**; losing it makes encrypted data unrecoverable. Database encryption is off by default, while the credential vault remains encrypted. Choose database encryption before creating data; changing the flag does not convert existing databases. One replica and Recreate updates support the single-writer workload; updates cause downtime. Stop all writers before maintenance or recovery.

- [Values reference](charts/wealthfolio/README.md) and [examples](examples/)
- [Backup and restore](docs/backup-restore.md) · [Upgrades and reinstall](docs/upgrading.md)
- [MCP](docs/mcp.md) · [Troubleshooting](docs/troubleshooting.md)
- [Security](docs/security.md) · [Compatibility](docs/compatibility.md)
- [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

The chart uses the official versioned Wealthfolio image pinned by digest. It does not rebuild or modify the application image. Independently authored chart and tooling files are Apache-2.0; Wealthfolio remains AGPL-3.0. See [NOTICE](NOTICE) and [image security](docs/security.md). Wealthfolio is a trademark of Teymz Inc.; no endorsement is implied.
