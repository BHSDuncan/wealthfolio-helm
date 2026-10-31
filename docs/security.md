# Security and data protection

## Credentials and the master key

The chart requires an existing Secret in the release namespace. It must contain a stable master key encoded as base64 text that decodes to 32 bytes, and an Argon2id PHC password hash. The chart renders only required Secret references; it does not create a Secret or read its contents through Helm `lookup`. Helm validates reference names, Kubernetes checks that the Secret and entries exist, and the application validates their contents. Kubernetes Secret base64 is not encryption: restrict RBAC and use cluster secret-at-rest protections.

Keep literal single `$` characters in Argon2 hashes; Compose-style `$$` escaping breaks them. Never put passwords, hashes, master keys or personal access tokens (PATs) in Helm values, command arguments, annotations, source or diagnostic reports. [Credential preparation](install.md#prepare-credentials) explains how to create the required files and Secret privately.

To change the login password, update the password-hash entry in the existing Secret and restart the Deployment. Set `WF_CONTEXT` and `WF_NAMESPACE` to the verified installation target, then run:

```bash
kubectl --context "$WF_CONTEXT" --namespace "$WF_NAMESPACE" \
  rollout restart deployment/wealthfolio
```

Replace `wealthfolio` if the Deployment has a different rendered name. The Recreate strategy stops the old pod before starting its replacement during a Deployment update. Manually deleting a pod does not provide that guarantee. For maintenance, scale to zero and wait for every pod using the claim to terminate.

**Retain the original master key independently of the cluster.** It derives signing, vault and optional database keys. Password changes cannot recover it, and automatic rotation is unsupported. Losing it loses encrypted data. Changing it breaks existing vault/database access. Keep keys needed by old backups. Restoration can resurrect old PATs: review and revoke tokens after restoring.

## Database encryption

Database encryption is **disabled by default**. Financial databases are then plaintext on storage, while the credential vault remains encrypted. To encrypt a new installation's databases, use the [fresh encrypted-install example](../examples/encrypted-fresh-install.yaml) before creating data.

Changing `encryption.required` does not convert existing databases. If the setting and a database's format disagree, the application refuses to open that profile. Profiles initialize lazily, so health and readiness checks can succeed even when a profile cannot open; confirm actual profile access when validating encryption.

Conversion is an offline operation: stop all writers, back up the complete data directory, check free space, and process every profile using upstream `db encrypt` or `db decrypt`. Retain the same operating-system user, image, original key and paths. The chart never runs conversion during upgrades. See [backup and recovery](backup-restore.md) before making changes.

## Pod and network protection

The pod runs as UID/GID 1000 with `fsGroup: 1000`, RuntimeDefault seccomp, a read-only root filesystem, no privilege escalation, all capabilities dropped and no mounted service-account token. Only `/data` and bounded, memory-backed `/tmp` are writable. The chart creates no privileged ownership-repair job or RBAC resources. Volume-driver support determines how `fsGroup` affects permissions. Existing root-owned data requires a backed-up offline repair that preserves access-control lists where possible.

Ingress access follows the existing controller and network configuration. Use TLS on networks you do not fully trust, configure the exact HTTPS origin, preserve trusted forwarded-protocol information and enable Secure cookies. Private access uses loopback port-forwarding or an authenticated SSH tunnel; its browser origin and cookie policy must match the installation. [MCP](mcp.md) is disabled by default, uses its own scoped PATs and audit log, and should remain private. Audit records can contain operational identifiers.

## Upstream image

The chart follows the official Wealthfolio image without rebuilding it: `ghcr.io/wealthfolio/wealthfolio:3.11.0@sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994`. Its recorded runtime is Alpine 3.19.9; [Alpine's support table](https://alpinelinux.org/releases/) lists normal 3.19 support ending on **2025-11-01**, followed by support on request.

Recorded Trivy 0.76.1 scans dated **2026-10-30** of that exact image reported the same ten OS-package findings on each architecture: two high, five medium and three low, covering four unique CVEs. The scanner identified fixes in BusyBox `1.36.1-r21` and musl `1.2.4_git20230717-r6`; the image contained older revisions. Findings describe installed packages, not demonstrated application exploits.

As of 2026-10-30, [upstream releases](https://github.com/wealthfolio/wealthfolio/releases) still listed 3.11.0 as latest and the [upstream Dockerfile](https://github.com/wealthfolio/wealthfolio/blob/main/Dockerfile) still selected Alpine 3.19. A newer supported official image was not established. This is an unresolved upstream runtime limitation, separate from chart validation. Review image support and [Alpine advisories](https://secdb.alpinelinux.org/) before public exposure; chart hardening cannot fix image vulnerabilities. Update the tag and digest together only after compatibility testing.

A newer base alone would not establish complete application safety: the server is statically linked, and OS-package scans do not cover all Rust/frontend/embedded dependencies. This project does not claim that the pinned image is vulnerability-free.
