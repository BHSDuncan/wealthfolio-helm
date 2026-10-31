# Wealthfolio community Helm chart

Unofficial community Helm chart for [Wealthfolio](https://wealthfolio.app/), maintained independently of the application. Chart **0.1.0** targets Wealthfolio **3.11.0**, Kubernetes **1.34+**, and Linux AMD64/ARM64. Helm 3 and 4 are supported. The official application image is pinned by digest.

## Install with Helm

Standard Helm installation is the primary supported workflow. You need Helm 3 or 4, kubectl with a trusted kubeconfig for an existing Kubernetes 1.34+ cluster, permission to create the release resources, and durable filesystem storage writable by UID/GID 1000. Nodes must be able to pull the official image from GHCR. The chart does not install host packages, Python environments, SSH tooling, a cluster or shared controllers. No ingress controller or cert-manager is needed for loopback access.

Versioned releases use `oci://ghcr.io/bhsduncan/charts/wealthfolio` with an explicit chart version; public downloads require no account or registry login. You can also install from `./charts/wealthfolio` in a source checkout or from a verified local chart archive. See the [release guide](https://github.com/BHSDuncan/wealthfolio-helm/blob/main/docs/releases.md) for versioning and distribution details.

### Prepare credentials

Follow the complete [credential preparation procedure](https://github.com/BHSDuncan/wealthfolio-helm/blob/main/docs/install.md#prepare-credentials) or your secret manager to create namespace `wealthfolio` and a namespace-local `wealthfolio-auth` Secret first:

| Secret entry | Required content |
|---|---|
| `WF_SECRET_KEY` | A stable master key: base64 text that decodes to exactly 32 random bytes. Back up the original independently. |
| `WF_AUTH_PASSWORD_HASH` | An Argon2id PHC password hash with literal single `$` characters. |

Never put a password, hash or master key in Helm values or shell arguments. The chart only references the Secret; it does not generate, read or rotate credentials. The upstream key format is distinct from the additional base64 encoding of Kubernetes Secret `data` entries; `kubectl --from-file`, a secret manager or `stringData` handles that layer. The linked procedure uses a private Python environment, interactive password input and file-based Secret creation without running the guided installer.

Save `values.yaml` with the minimum nonsecret configuration:

```yaml
auth:
  existingSecret: wealthfolio-auth
```

Only `auth.existingSecret` is required in chart values. Durable default storage must be available; customize `persistence.storageClass` when necessary. Shell environment variables do **not** automatically become Helm values or create Secrets. Helm uses defaults and the values files/explicit overrides passed to it; never supply credential contents as overrides.

### Install and connect

The commands below install chart `0.1.0`. Replace `your-context` with your verified context. The preview explicitly selects the Kubernetes 1.34 support floor; installation validates the actual cluster version:

```bash
helm template wealthfolio oci://ghcr.io/bhsduncan/charts/wealthfolio \
  --version 0.1.0 --namespace wealthfolio \
  --kube-version 1.34.0 --values values.yaml
helm install wealthfolio oci://ghcr.io/bhsduncan/charts/wealthfolio \
  --version 0.1.0 --kube-context your-context --namespace wealthfolio \
  --values values.yaml --wait --timeout 10m
```

For local installation, replace the OCI reference with `./charts/wealthfolio` from the repository root, or with your verified archive path, and omit `--version`. Local source can also be checked with `helm lint ./charts/wealthfolio --strict --values values.yaml`.

The default Service is private ClusterIP. For local access, keep this command running and open **http://localhost:8088**:

```bash
kubectl --context your-context --namespace wealthfolio \
  port-forward service/wealthfolio 8088:8088 --address 127.0.0.1
```

### Optional ingress and TLS

To use an existing Ingress controller, save these nonsecret values as `access.yaml`, replacing the hostname, class and TLS Secret name. Point DNS at that controller and provide a namespace-local TLS Secret with a certificate that covers your hostname and is trusted by your browser:

```yaml
auth:
  existingSecret: wealthfolio-auth
  cookieSecure: "true"
ingress:
  enabled: true
  className: your-ingress-class
  hosts:
    - wealthfolio.example.com
  tls:
    - secretName: wealthfolio-tls
      hosts:
        - wealthfolio.example.com
app:
  corsAllowOrigins:
    - https://wealthfolio.example.com
```

Pass both `--values values.yaml --values access.yaml` to Helm so your base configuration is retained. The controller must preserve the hostname and report the original request protocol through a trusted forwarded header. The application requires a root `/` route; subpath hosting is unsupported.

Manage DNS, certificates and ingress controllers separately. Cert-manager is optional; the chart never installs controllers, issuers or Certificate resources. An existing TLS Secret is sufficient. See the full [installation guide](https://github.com/BHSDuncan/wealthfolio-helm/blob/main/docs/install.md#optional-ingress-and-tls) for access choices.

The chart does not select a node address or install a network controller. Set `service.port` independently of the fixed application port 8088; an explicit `service.nodePort` must fit your cluster's configured range, which may differ from Kubernetes' default 30000–32767. Configure `app.corsAllowOrigins` with the exact browser scheme, host and external port when using a nonstandard ingress/LB port. Ingress hosts are DNS names; use a Service and an exact IP origin for direct IP access instead.

### IPv6

For IPv6 Pod networking, set `app.listenAddress: "::"`; the application then binds `[::]:8088`. The default remains the IPv4 wildcard `0.0.0.0`. Verify your CNI, ingress controller and node dual-stack behavior before relying on both families. IPv6 literals in browser origins and MCP allowlists use brackets, for example `https://[2001:db8::1]:8443` and `[2001:db8::1]:8443`. Replace this documentation-only address with your own. Local port-forwarding remains loopback-only.

## Optional guided installer

The full repository also includes an interactive convenience installer for homelab users and first-time Kubernetes operators. It is optional and is not part of this chart archive. It prepares credentials and missing Helm/Python dependencies outside Helm, saves private recovery state, and supports explicit access reconfiguration, certificate renewal and optional SSH. Read its [prerequisites, host changes, saved files and management workflow](https://github.com/BHSDuncan/wealthfolio-helm/blob/main/docs/install.md#optional-guided-installer) before use. Use installer commands for its supported access changes, renewal, reconnect and retry operations while it manages an installation; ordinary reruns preserve settings and do not upgrade the application.

## Storage, security and lifecycle

### One writer and complete backups

The Deployment runs exactly one replica and uses the `Recreate` strategy, so upgrades cause downtime. This strategy stops old pods before starting new ones during Deployment upgrades. Manual Pod deletion, forced deletion and node failures can still overlap writers. Before maintenance, scale the Deployment to zero and wait until every pod using the claim has terminated. ReadWriteOnce alone does not prevent another writer on the same node. Never share a claim between live releases.

The whole `/data` tree is persistent: profile registry, every profile database and sidecar, encrypted vault, backups and addons. Back up the entire stopped tree, keep its original master key separately, and verify authenticated recovery into empty test storage before relying on a backup. Helm health checks are shallow; they do not establish persistence or recovery.

**Database encryption is off by default.** Set `encryption.required: true` before creating data if you want encrypted databases. The vault uses the master key either way. Changing this flag does not convert existing files. Offline conversion must process every profile with all writers stopped, a complete backup and the original image, runtime user and key. Never replace the master key as an upgrade or repair.

### Retained storage and upgrades

A chart-created PVC is retained on ordinary uninstall through `helm.sh/resource-policy: keep`. Keep the namespace and external Secret. Reinstall with `persistence.existingClaim` set to the verified retained claim and the original key/encryption policy. With an existing claim, provisioning values are ignored. Namespace deletion, claim deletion, reclaim policies or local-path host loss can destroy data; retention is not backup. Do not shrink a claim or change its bound storage class/access mode.

Before application upgrades, review [upstream releases](https://github.com/wealthfolio/wealthfolio/releases), image advisories and migration requirements, back up stopped data, and verify recovery. Helm rollback restores chart manifests, not databases or master keys. The pinned 3.11.0 image has a recorded Alpine 3.19 base-image security limitation; review the full repository's security guidance before public exposure. Chart hardening cannot fix upstream image vulnerabilities.

### Runtime security and MCP

The application runs as UID/GID 1000 with a read-only root filesystem, dropped capabilities, RuntimeDefault seccomp and no service-account token. Only persistent `/data` and bounded memory-backed `/tmp` are writable. Storage-driver permission behavior remains environment-specific; no privileged ownership repair is installed.

MCP is disabled by default. Enabling it requires a nonwildcard Host allowlist and a scoped upstream personal access token; browser passwords and cookies are not MCP bearer credentials. Audit is enabled by default and belongs to the protected whole-data backup. Create expiring read-only tokens in Settings → AI Agent Access and revoke them after use.

Image overrides require a digest; change tag and digest together after testing. `extraEnv` permits only literal `RUST_LOG` or a Secret-backed `METAL_PRICE_API_KEY`. Arbitrary sidecars, command overrides and authentication bypasses are not supported. Generated Helm/Kubernetes identity labels and ownership annotations are reserved.

## Values reference

Only `auth.existingSecret` must be supplied for a basic installation. The tables below cover Wealthfolio settings and the chart's operational constraints. See [values.yaml](values.yaml) for all defaults and conventional Kubernetes options, and [values.schema.json](values.schema.json) for accepted types, structures and bounds. Helm validates values and cross-field rules; it cannot verify the contents of cluster Secrets or storage.

### Wealthfolio settings

Credential values belong in the existing Secret described under [Install with Helm](#install-with-helm), never in these values.

| Value | Default | Purpose |
|---|---|---|
| `auth.existingSecret` | `""` | **Required:** name of the existing authentication Secret in the release namespace. |
| `auth.secretKeys.secretKey` | `WF_SECRET_KEY` | Secret entry containing the stable master key. Retain the original key independently of the cluster. |
| `auth.secretKeys.passwordHash` | `WF_AUTH_PASSWORD_HASH` | Secret entry containing the literal Argon2id PHC password hash. |
| `auth.tokenTtlMinutes` | `60` | Authentication session lifetime in minutes. |
| `auth.cookieSecure` | `"auto"` | Secure-cookie policy: `"auto"`, `"true"` or `"false"`. Use `"true"` for HTTPS ingress. |
| `app.listenAddress` | `"0.0.0.0"` | IPv4 wildcard listener; use `"::"` for IPv6. The application port remains 8088. |
| `app.corsAllowOrigins` | `["http://localhost:8088", "http://127.0.0.1:8088"]` | Exact browser origins, including scheme and external port; no wildcard or path. |
| `app.requestTimeoutMs` | `300000` | API request timeout in milliseconds. |
| `app.logFormat` | `"text"` | Server log format: `"text"` or `"json"`. |
| `encryption.required` | `false` | Require encrypted databases. Choose before creating data; changing the flag does not convert existing files. The vault always requires the original master key. |
| `mcp.enabled` | `false` | Enable the MCP endpoint; access requires a scoped personal access token. |
| `mcp.auditEnabled` | `true` | Record MCP audit events with the application data. |
| `mcp.allowedHosts` | `[]` | Required when MCP is enabled. Explicit HTTP Host allowlist, optionally including ports; no wildcard. |
| `extraEnv` | `[]` | Only literal `RUST_LOG` and Secret-backed `METAL_PRICE_API_KEY` entries are supported. Provider credentials must use `valueFrom.secretKeyRef` with `optional: false`, never a literal value. |

### Chart safeguards and storage

See [Storage, security and lifecycle](#storage-security-and-lifecycle) before changing storage, encryption or images, and before maintenance or recovery.

| Value | Default | Constraint or behavior |
|---|---|---|
| `replicaCount` | `1` | Exactly one writer; the Deployment uses `Recreate`, with upgrade downtime. |
| `image.repository`, `image.tag`, `image.digest` | Official Wealthfolio `3.11.0`, digest-pinned | Image overrides require a digest. Review compatibility and update tag and digest together. The exact defaults are in [values.yaml](values.yaml). |
| `persistence.existingClaim` | `""` | Use an existing namespace-local claim, or leave empty to create a retained claim for the whole `/data` tree. Never share it with another live writer. |
| `persistence.size`, `persistence.storageClass`, `persistence.accessModes` | `5Gi`, `null`, `["ReadWriteOnce"]` | New-claim provisioning only; ignored with `existingClaim`. A null class uses the cluster default; `""` requests no class. Do not shrink a claim or change its bound class/access modes. Retention is not backup. |
| `podSecurityContext`, `securityContext` | UID/GID/fsGroup 1000 with hardened defaults | Storage must support this identity. The schema enforces nonroot execution and security constraints; no privileged ownership repair is installed. |
| `tmp.sizeLimit` | `64Mi` | Size limit for the memory-backed writable `/tmp` volume. |
| `tests.enabled` | `true` | Render an unauthenticated Helm health-test hook. A passing health check does not establish persistence or recovery. |
| `tests.image` | Digest-pinned `curlimages/curl:8.23.0` | Health-test utility image. Review its digest when changing the tag. |

### Standard Kubernetes and Helm options

These use conventional Kubernetes structures; their defaults and accepted fields are in [values.yaml](values.yaml) and [values.schema.json](values.schema.json).

- **Exposure:** `service` and `ingress` configure ports, Service type, hosts, TLS and controller annotations. Ingress is optional; controllers and certificates are managed separately.
- **Resources and health:** `resources`, `probes` and `terminationGracePeriodSeconds` configure sizing, health checks and shutdown timing.
- **Scheduling:** `nodeSelector`, `tolerations` and `affinity` configure Pod placement.
- **Names and metadata:** `nameOverride`, `fullnameOverride`, labels and annotations customize resource identity and metadata. Keep names stable; changing them can select a new claim. Generated identity labels and ownership annotations are reserved.
- **Image pulling:** `image.pullPolicy` and `imagePullSecrets` configure image retrieval and existing namespace-local registry credentials.

## Maintenance and licensing

Chart and application versions are independent. The full repository contains contribution, upgrade, backup/recovery, security-reporting and release procedures. Publication is a separate maintainer action; validation does not publish a chart or application image.

Independently authored chart, tooling, tests and documentation use [Apache-2.0](LICENSE), which includes an express patent license. Wealthfolio remains separately licensed under [AGPL-3.0](https://github.com/wealthfolio/wealthfolio/blob/d1fda0b65f69d8557b1ef80e555c799215ad28b3/LICENSE). This chart references the unmodified official image; its archive contains no application binary, source, assets or upstream artwork. See [NOTICE](NOTICE) for attribution and scope. Wealthfolio is a trademark of Teymz Inc.; no endorsement is implied.
