# Wealthfolio community Helm chart

Unofficial community Helm chart for [Wealthfolio](https://wealthfolio.app/), maintained independently of the application. Chart **0.1.0** targets Wealthfolio **3.11.0**, Kubernetes **1.34+**, and Linux AMD64/ARM64. Helm 3 and 4 are supported. The official application image is pinned by digest.

## Install and access

This README also ships inside the chart archive. For the guided Kubernetes installer, download or clone the complete project repository and follow its root README. The installer runs locally by default using the current context from a readable kubeconfig shared by kubectl and Helm. It creates credentials interactively. Explicit target overrides and optional SSH execution are also supported. Installer scripts and extended operator guides are intentionally outside this chart package. No public chart registry is configured yet.

For direct Helm use, first create a namespace and use your secret-management workflow to create a namespace-local `wealthfolio-auth` Secret with these entries:

| Secret entry | Required content |
|---|---|
| `WF_SECRET_KEY` | A stable master key: base64 text that decodes to exactly 32 random bytes. Back up the original independently. |
| `WF_AUTH_PASSWORD_HASH` | An Argon2id PHC password hash with literal single `$` characters. |

Never put a password, hash or master key in Helm values or shell arguments. The chart only references the Secret; it does not generate, read or rotate credentials. The upstream key format is distinct from the additional base64 encoding of Kubernetes Secret `data` entries; a secret manager or `stringData` accepts the application value directly.

Install a downloaded chart archive with your verified context and namespace:

```bash
helm install wealthfolio ./wealthfolio-0.1.0.tgz \
  --kube-context your-context --namespace wealthfolio \
  --set auth.existingSecret=wealthfolio-auth --wait --timeout 10m
```

From the complete source checkout, use `./charts/wealthfolio` in place of the archive path. Only `auth.existingSecret` is required in chart values. Durable default storage must be available and writable by UID/GID 1000; customize `persistence.storageClass` when necessary.

The default Service is private ClusterIP. For local access, keep this command running and open **http://localhost:8088**:

```bash
kubectl --context your-context --namespace wealthfolio \
  port-forward service/wealthfolio 8088:8088 --address 127.0.0.1
```

To use an existing Ingress controller instead, save these nonsecret values as `access.yaml`, replacing the hostname and class. Point DNS at that controller and provide an existing namespace-local TLS Secret trusted by your browser:

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

Pass `--values access.yaml` to Helm. Ingress must preserve the hostname and trusted forwarded protocol. The chart creates a root `/` route; DNS, certificates, controller installation and subpath rewrites are outside its scope. The guided installer in the full repository can provision a private CA or use an existing cert-manager issuer.

The chart does not select a node address or install a network controller. Set `service.port` independently of the fixed application port 8088; an explicit `service.nodePort` must fit your cluster's configured range, which may differ from Kubernetes' default 30000–32767. Configure `app.corsAllowOrigins` with the exact browser scheme, host and external port when using a nonstandard ingress/LB port. Ingress hosts are DNS names; use a Service and an exact IP origin for direct IP access instead. Subpath hosting is unsupported by the packaged application.

For IPv6 Pod networking, set `app.listenAddress: "::"`; the application then binds `[::]:8088`. The default remains the IPv4 wildcard `0.0.0.0`. Verify your CNI/controller and node dual-stack behavior before relying on both families. IPv6 literals in browser origins and MCP allowlists use brackets, for example `https://[2001:db8::1]:8443` and `[2001:db8::1]:8443`. The latter is a documentation address, not a usable deployment target. Local port-forwarding remains loopback-only.

## Storage, security and lifecycle

Exactly one replica uses `Recreate`; upgrades cause downtime. This strategy stops old pods before new ones during Deployment upgrades. Manual Pod deletion, forced deletion and node failures can still overlap writers. Before maintenance, scale the Deployment to zero and wait until every pod using the claim has terminated. ReadWriteOnce alone does not prevent another writer on the same node. Never share a claim between live releases.

The whole `/data` tree is persistent: profile registry, every profile database and sidecar, encrypted vault, backups and addons. Back up the entire stopped tree, keep its original master key separately, and verify authenticated recovery into empty test storage before relying on a backup. Helm health checks are shallow; they do not establish persistence or recovery.

**Database encryption is off by default.** Set `encryption.required: true` before creating data if you want encrypted databases. The vault uses the master key either way. Changing this flag does not convert existing files; offline per-profile conversion requires stopped writers, a complete backup and the original image/user/key. Never replace the master key as an upgrade or repair.

A chart-created PVC is retained on ordinary uninstall through `helm.sh/resource-policy: keep`. Keep the namespace and external Secret. Reinstall with `persistence.existingClaim` set to the verified retained claim and the original key/encryption policy. With an existing claim, provisioning values are ignored. Namespace deletion, claim deletion, reclaim policies or local-path host loss can destroy data; retention is not backup. Do not shrink a claim or change its bound storage class/access mode.

Before application upgrades, review [upstream releases](https://github.com/wealthfolio/wealthfolio/releases), image advisories and migration requirements, back up stopped data, and verify recovery. Helm rollback restores chart manifests, not databases or master keys. The pinned 3.11.0 image has a recorded Alpine 3.19 base-image security limitation; review the full repository's security guidance before public exposure. Chart hardening cannot fix upstream image vulnerabilities.

The application runs as UID/GID 1000 with a read-only root filesystem, dropped capabilities, RuntimeDefault seccomp and no service-account token. Only persistent `/data` and bounded memory-backed `/tmp` are writable. Storage-driver permission behavior remains environment-specific; no privileged ownership repair is installed.

MCP is disabled by default. Enabling it requires a nonwildcard Host allowlist and a scoped upstream personal access token; browser passwords and cookies are not MCP bearer credentials. Audit is enabled by default and belongs to the protected whole-data backup. Create expiring read-only tokens in Settings → AI Agent Access and revoke them after use.

Image overrides require a digest; change tag and digest together after testing. `extraEnv` permits only literal `RUST_LOG` or a Secret-backed `METAL_PRICE_API_KEY`. Arbitrary sidecars, command overrides and authentication bypasses are not supported. Generated Helm/Kubernetes identity labels and ownership annotations are reserved.

## Values reference

Defaults are from `values.yaml`; object/list item descriptions also document the accepted structure. Fixed objects reject unknown properties. Nullable `storageClass`/`nodePort` may be absent after Helm merges values. The schema enforces types, enums, positive quantities and bounds, while template checks enforce cross-field rules. Reference validation cannot inspect cluster Secret/storage contents.

| Value | Type | Default | Meaning |
|---|---|---|---|
| `nameOverride` | string | `""` | Optional stable short chart name. |
| `fullnameOverride` | string | `""` | Explicit stable resource name; changes can select a new PVC. |
| `replicaCount` | integer | `1` | Exactly one writer is supported. |
| `image` | object | `{"repository":"ghcr.io/wealthfolio/wealthfolio","tag":"3.11.0","digest":"sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994","pullPolicy":"IfNotPresent"}` | Official upstream image pinned to its multiarchitecture digest. |
| `image.repository` | string | `"ghcr.io/wealthfolio/wealthfolio"` | Container image repository without tag or digest. |
| `image.tag` | string | `"3.11.0"` | Application version matching the selected digest. |
| `image.digest` | string | `"sha256:3e996e9b3fbf9c945eca89e7b89eb03e0966beb1c51b977ccccb811f76ae2994"` | Immutable image digest; update it when changing the image. |
| `image.pullPolicy` | string | `"IfNotPresent"` | Kubernetes image pull policy. |
| `imagePullSecrets` | array | `[]` | Existing namespace image-pull Secrets. |
| `imagePullSecrets[]` | object | — | Secret reference. |
| `imagePullSecrets[].name` | string | — | Existing Secret name. |
| `auth` | object | `{"existingSecret":"","secretKeys":{"secretKey":"WF_SECRET_KEY","passwordHash":"WF_AUTH_PASSWORD_HASH"},"tokenTtlMinutes":60,"cookieSecure":"auto"}` | Existing authentication Secret and session policy. |
| `auth.existingSecret` | string | `""` | Required existing namespaced Secret; chart never reads or creates it. |
| `auth.secretKeys` | object | `{"secretKey":"WF_SECRET_KEY","passwordHash":"WF_AUTH_PASSWORD_HASH"}` | Required keys in existing Secret. |
| `auth.secretKeys.secretKey` | string | `"WF_SECRET_KEY"` | 32-byte base64 master-key entry. |
| `auth.secretKeys.passwordHash` | string | `"WF_AUTH_PASSWORD_HASH"` | Literal Argon2id PHC hash entry. |
| `auth.tokenTtlMinutes` | integer | `60` | Session lifetime; supported practical bound is one year. |
| `auth.cookieSecure` | string | `"auto"` | Secure cookie policy; HTTPS ingress should use true. |
| `app` | object | `{"listenAddress":"0.0.0.0","corsAllowOrigins":["http://localhost:8088","http://127.0.0.1:8088"],"requestTimeoutMs":300000,"logFormat":"text"}` | Verified server runtime settings. |
| `app.listenAddress` | string | `"0.0.0.0"` | Pod listener wildcard: IPv4 or `::` for IPv6; container port stays 8088. |
| `app.corsAllowOrigins` | array | `["http://localhost:8088","http://127.0.0.1:8088"]` | Exact browser origins without wildcard, credentials or path. |
| `app.requestTimeoutMs` | integer | `300000` | API request timeout in milliseconds. |
| `app.logFormat` | string | `"text"` | Server log format. |
| `encryption` | object | `{"required":false}` | Database encryption policy; no automatic conversion. |
| `encryption.required` | boolean | `false` | Require encrypted databases; false creates plaintext databases. |
| `persistence` | object | `{"existingClaim":"","size":"5Gi","storageClass":null,"accessModes":["ReadWriteOnce"],"annotations":{}}` | Whole /data storage; existingClaim ignores creation settings. |
| `persistence.existingClaim` | string | `""` | Existing namespace claim, or empty to create a retained claim. |
| `persistence.size` | string | `"5Gi"` | Positive new-claim capacity; shrinking is unsupported. |
| `persistence.storageClass` | null/string | `null` | Null omits class, empty explicitly selects no class, named uses existing class. |
| `persistence.accessModes` | array | `["ReadWriteOnce"]` | Exactly one filesystem access mode; RWOP requires CSI support. |
| `persistence.annotations` | object | `{}` | Extra PVC annotations; Helm ownership/keep keys are reserved. |
| `service` | object | `{"type":"ClusterIP","port":8088,"nodePort":null,"annotations":{}}` | Service; public exposure requires explicit choice. |
| `service.type` | string | `"ClusterIP"` | Kubernetes Service type. |
| `service.port` | integer | `8088` | Service port; container target stays named http/8088. |
| `service.nodePort` | null/integer | `null` | Only NodePort Services; must fit the target cluster configured node-port range. |
| `service.annotations` | object | `{}` | Nonsecret Service annotations. |
| `ingress` | object | `{"enabled":false,"className":"","annotations":{},"hosts":[],"tls":[]}` | Optional standard root-path Ingress; no controller is installed. |
| `ingress.enabled` | boolean | `false` | Create an Ingress. |
| `ingress.className` | string | `""` | Existing controller class or empty for its default. |
| `ingress.annotations` | object | `{}` | Existing-controller annotations; no template evaluation. |
| `ingress.hosts` | array | `[]` | DNS hosts required when enabled. |
| `ingress.tls` | array | `[]` | Existing TLS Secret references. |
| `ingress.tls[]` | object | — | TLS mapping. |
| `ingress.tls[].secretName` | string | — | Optional namespace TLS Secret; omit to use the controller certificate. |
| `ingress.tls[].hosts` | array | — | Hosts must also occur in ingress.hosts. |
| `mcp` | object | `{"enabled":false,"auditEnabled":true,"allowedHosts":[]}` | Optional in-process MCP endpoint; PATs stay outside Helm. |
| `mcp.enabled` | boolean | `false` | Enable /mcp with upstream PAT authentication. |
| `mcp.auditEnabled` | boolean | `true` | Enable upstream MCP audit logging. |
| `mcp.allowedHosts` | array | `[]` | Explicit Host-header allowlist required when enabled; no wildcard. |
| `resources` | object | `{"requests":{"cpu":"100m","memory":"256Mi"},"limits":{"memory":"1Gi"}}` | Initial recommendations; adjust using measured workload. |
| `resources.requests` | object | `{"cpu":"100m","memory":"256Mi"}` | Requested resources. |
| `resources.requests.cpu` | string | `"100m"` | Positive CPU quantity. |
| `resources.requests.memory` | string | `"256Mi"` | Positive memory quantity. |
| `resources.limits` | object | `{"memory":"1Gi"}` | Resource caps; no CPU cap by default. |
| `resources.limits.memory` | string | `"1Gi"` | Positive memory limit; includes memory-backed tmp. |
| `resources.limits.cpu` | string | — | Optional positive CPU limit. |
| `podSecurityContext` | object | `{"runAsNonRoot":true,"runAsUser":1000,"runAsGroup":1000,"fsGroup":1000,"fsGroupChangePolicy":"OnRootMismatch","seccompProfile":{"type":"RuntimeDefault"}}` | Supported numeric runtime identity and pod security. |
| `podSecurityContext.runAsNonRoot` | boolean | `true` | Required non-root runtime. |
| `podSecurityContext.runAsUser` | integer | `1000` | Supported runtime UID. |
| `podSecurityContext.runAsGroup` | integer | `1000` | Supported runtime GID. |
| `podSecurityContext.fsGroup` | integer | `1000` | Supported data filesystem group. |
| `podSecurityContext.fsGroupChangePolicy` | string | `"OnRootMismatch"` | Driver-sensitive ownership handling. |
| `podSecurityContext.seccompProfile` | object | `{"type":"RuntimeDefault"}` | Supported seccomp profile. |
| `podSecurityContext.seccompProfile.type` | string | `"RuntimeDefault"` | Runtime default seccomp. |
| `securityContext` | object | `{"allowPrivilegeEscalation":false,"readOnlyRootFilesystem":true,"capabilities":{"drop":["ALL"]}}` | Container security invariants. |
| `securityContext.allowPrivilegeEscalation` | boolean | `false` | Privilege escalation forbidden. |
| `securityContext.readOnlyRootFilesystem` | boolean | `true` | Root filesystem must be read-only. |
| `securityContext.capabilities` | object | `{"drop":["ALL"]}` | Capability policy. |
| `securityContext.capabilities.drop` | array | `["ALL"]` | Drop every capability. |
| `tmp` | object | `{"sizeLimit":"64Mi"}` | Writable memory-backed scratch space. |
| `tmp.sizeLimit` | string | `"64Mi"` | Positive temporary-storage bound. |
| `probes` | object | `{"startup":{"enabled":true,"initialDelaySeconds":0,"periodSeconds":5,"timeoutSeconds":2,"failureThreshold":60},"readiness":{"enabled":true,"periodSeconds":10,"timeoutSeconds":2,"failureThreshold":3},"liveness":{"enabled":true,"periodSeconds":30,"timeoutSeconds":2,"failureThreshold":3}}` | Unauthenticated shallow health checks; not integrity tests. |
| `probes.startup` | object | `{"enabled":true,"initialDelaySeconds":0,"periodSeconds":5,"timeoutSeconds":2,"failureThreshold":60}` | Startup healthz; default budget approximately five minutes. |
| `probes.startup.enabled` | boolean | `true` | Enable this HTTP probe. |
| `probes.startup.periodSeconds` | integer | `5` | Seconds between checks. |
| `probes.startup.timeoutSeconds` | integer | `2` | HTTP timeout in seconds. |
| `probes.startup.failureThreshold` | integer | `60` | Consecutive failures before action. |
| `probes.startup.initialDelaySeconds` | integer | `0` | Delay before first startup check. |
| `probes.readiness` | object | `{"enabled":true,"periodSeconds":10,"timeoutSeconds":2,"failureThreshold":3}` | Readiness readyz. |
| `probes.readiness.enabled` | boolean | `true` | Enable this HTTP probe. |
| `probes.readiness.periodSeconds` | integer | `10` | Seconds between checks. |
| `probes.readiness.timeoutSeconds` | integer | `2` | HTTP timeout in seconds. |
| `probes.readiness.failureThreshold` | integer | `3` | Consecutive failures before action. |
| `probes.liveness` | object | `{"enabled":true,"periodSeconds":30,"timeoutSeconds":2,"failureThreshold":3}` | Liveness healthz. |
| `probes.liveness.enabled` | boolean | `true` | Enable this HTTP probe. |
| `probes.liveness.periodSeconds` | integer | `30` | Seconds between checks. |
| `probes.liveness.timeoutSeconds` | integer | `2` | HTTP timeout in seconds. |
| `probes.liveness.failureThreshold` | integer | `3` | Consecutive failures before action. |
| `terminationGracePeriodSeconds` | integer | `30` | Shutdown grace; upstream does not promise request draining. |
| `commonLabels` | object | `{}` | Additional labels; generated Kubernetes/Helm labels are reserved. |
| `podLabels` | object | `{}` | Additional pod labels; selectors and generated labels are reserved. |
| `podAnnotations` | object | `{}` | Additional nonsecret pod annotations; ownership keys reserved. |
| `nodeSelector` | object | `{}` | Standard exact node placement labels. |
| `tolerations` | array | `[]` | Standard Kubernetes placement tolerations. |
| `tolerations[]` | object | — | Toleration. |
| `tolerations[].key` | string | — | Taint key. |
| `tolerations[].operator` | string | — | Toleration operator. |
| `tolerations[].value` | string | — | Taint value. |
| `tolerations[].effect` | string | — | Taint effect. |
| `tolerations[].tolerationSeconds` | integer | — | NoExecute duration. |
| `affinity` | object | `{}` | Standard Kubernetes node/pod affinity. |
| `affinity.nodeAffinity` | object | — | Node affinity. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution` | object | — | Required node selector. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms` | array | — | Node selector terms. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[]` | object | — | Node selector term. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchExpressions` | array | — | Node label requirements. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchExpressions[]` | object | — | Node selector expression. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchExpressions[].key` | string | — | Node label or field. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchExpressions[].operator` | string | — | Node selector operator. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchExpressions[].values` | array | — | Expression values. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchFields` | array | — | Node field requirements. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchFields[]` | object | — | Node selector expression. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchFields[].key` | string | — | Node label or field. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchFields[].operator` | string | — | Node selector operator. |
| `affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms[].matchFields[].values` | array | — | Expression values. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution` | array | — | Weighted node preference. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[]` | object | — | Weighted node term. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].weight` | integer | — | Weight. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference` | object | — | Node selector term. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchExpressions` | array | — | Node label requirements. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchExpressions[]` | object | — | Node selector expression. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchExpressions[].key` | string | — | Node label or field. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchExpressions[].operator` | string | — | Node selector operator. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchExpressions[].values` | array | — | Expression values. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchFields` | array | — | Node field requirements. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchFields[]` | object | — | Node selector expression. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchFields[].key` | string | — | Node label or field. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchFields[].operator` | string | — | Node selector operator. |
| `affinity.nodeAffinity.preferredDuringSchedulingIgnoredDuringExecution[].preference.matchFields[].values` | array | — | Expression values. |
| `affinity.podAffinity` | object | — | Pod affinity selection. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution` | array | — | Required pod placement terms. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[]` | object | — | Pod affinity term. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector` | object | — | Kubernetes label selector. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchLabels` | object | — | Exact label matches. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions` | array | — | Label requirements. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions[]` | object | — | Label selector expression. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions[].key` | string | — | Label key. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions[].operator` | string | — | Selector operator. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions[].values` | array | — | Selector values. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector` | object | — | Kubernetes label selector. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchLabels` | object | — | Exact label matches. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions` | array | — | Label requirements. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions[]` | object | — | Label selector expression. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions[].key` | string | — | Label key. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions[].operator` | string | — | Selector operator. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions[].values` | array | — | Selector values. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaces` | array | — | Namespace names. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].topologyKey` | string | — | Topology label key. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].matchLabelKeys` | array | — | Pod label keys. |
| `affinity.podAffinity.requiredDuringSchedulingIgnoredDuringExecution[].mismatchLabelKeys` | array | — | Pod label exclusion keys. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution` | array | — | Weighted pod placement terms. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[]` | object | — | Weighted pod placement. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].weight` | integer | — | Weight. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm` | object | — | Pod affinity term. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector` | object | — | Kubernetes label selector. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchLabels` | object | — | Exact label matches. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions` | array | — | Label requirements. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions[]` | object | — | Label selector expression. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions[].key` | string | — | Label key. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions[].operator` | string | — | Selector operator. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions[].values` | array | — | Selector values. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector` | object | — | Kubernetes label selector. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchLabels` | object | — | Exact label matches. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions` | array | — | Label requirements. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions[]` | object | — | Label selector expression. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions[].key` | string | — | Label key. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions[].operator` | string | — | Selector operator. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions[].values` | array | — | Selector values. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaces` | array | — | Namespace names. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.topologyKey` | string | — | Topology label key. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.matchLabelKeys` | array | — | Pod label keys. |
| `affinity.podAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.mismatchLabelKeys` | array | — | Pod label exclusion keys. |
| `affinity.podAntiAffinity` | object | — | Pod affinity selection. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution` | array | — | Required pod placement terms. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[]` | object | — | Pod affinity term. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector` | object | — | Kubernetes label selector. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchLabels` | object | — | Exact label matches. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions` | array | — | Label requirements. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions[]` | object | — | Label selector expression. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions[].key` | string | — | Label key. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions[].operator` | string | — | Selector operator. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].labelSelector.matchExpressions[].values` | array | — | Selector values. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector` | object | — | Kubernetes label selector. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchLabels` | object | — | Exact label matches. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions` | array | — | Label requirements. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions[]` | object | — | Label selector expression. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions[].key` | string | — | Label key. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions[].operator` | string | — | Selector operator. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaceSelector.matchExpressions[].values` | array | — | Selector values. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].namespaces` | array | — | Namespace names. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].topologyKey` | string | — | Topology label key. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].matchLabelKeys` | array | — | Pod label keys. |
| `affinity.podAntiAffinity.requiredDuringSchedulingIgnoredDuringExecution[].mismatchLabelKeys` | array | — | Pod label exclusion keys. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution` | array | — | Weighted pod placement terms. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[]` | object | — | Weighted pod placement. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].weight` | integer | — | Weight. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm` | object | — | Pod affinity term. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector` | object | — | Kubernetes label selector. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchLabels` | object | — | Exact label matches. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions` | array | — | Label requirements. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions[]` | object | — | Label selector expression. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions[].key` | string | — | Label key. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions[].operator` | string | — | Selector operator. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.labelSelector.matchExpressions[].values` | array | — | Selector values. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector` | object | — | Kubernetes label selector. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchLabels` | object | — | Exact label matches. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions` | array | — | Label requirements. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions[]` | object | — | Label selector expression. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions[].key` | string | — | Label key. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions[].operator` | string | — | Selector operator. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaceSelector.matchExpressions[].values` | array | — | Selector values. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.namespaces` | array | — | Namespace names. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.topologyKey` | string | — | Topology label key. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.matchLabelKeys` | array | — | Pod label keys. |
| `affinity.podAntiAffinity.preferredDuringSchedulingIgnoredDuringExecution[].podAffinityTerm.mismatchLabelKeys` | array | — | Pod label exclusion keys. |
| `extraEnv` | array | `[]` | Only RUST_LOG literal or METAL_PRICE_API_KEY Secret reference. |
| `extraEnv[0]` | object | — | Literal logging filter. |
| `extraEnv[0].name` | string | — | Logging variable. |
| `extraEnv[0].value` | string | — | Nonempty log filter. |
| `extraEnv[1]` | object | — | External provider key reference. |
| `extraEnv[1].name` | string | — | Provider key variable. |
| `extraEnv[1].valueFrom` | object | — | Existing Secret source. |
| `extraEnv[1].valueFrom.secretKeyRef` | object | — | Nonoptional Secret key. |
| `extraEnv[1].valueFrom.secretKeyRef.name` | string | — | Existing namespace Secret. |
| `extraEnv[1].valueFrom.secretKeyRef.key` | string | — | Secret key name. |
| `extraEnv[1].valueFrom.secretKeyRef.optional` | boolean | — | Key must exist. |
| `tests` | object | `{"enabled":true,"image":{"repository":"docker.io/curlimages/curl","tag":"8.23.0","digest":"sha256:3fb7e13e5d179a845c97d78204f3f1064d1907037f26a3ebf91710571151fd93"}}` | Unauthenticated Helm health test; no credential/PVC mount. |
| `tests.enabled` | boolean | `true` | Render the Helm test hook. |
| `tests.image` | object | `{"repository":"docker.io/curlimages/curl","tag":"8.23.0","digest":"sha256:3fb7e13e5d179a845c97d78204f3f1064d1907037f26a3ebf91710571151fd93"}` | Pinned curl health utility image. |
| `tests.image.repository` | string | `"docker.io/curlimages/curl"` | Registry/repository without tag or digest. |
| `tests.image.tag` | string | `"8.23.0"` | Explicit reviewed tag; changing it requires clearing or reviewing digest. |
| `tests.image.digest` | string | `"sha256:3fb7e13e5d179a845c97d78204f3f1064d1907037f26a3ebf91710571151fd93"` | Reviewed SHA256 digest, or empty for tag-only selection. |

## Maintenance and licensing

Chart and application versions are independent. The full repository contains contribution, upgrade, backup/recovery, security-reporting and release procedures. Publication is a separate maintainer action; validation does not publish a chart or application image.

Independently authored chart, tooling, tests and documentation use [Apache-2.0](LICENSE), which includes an express patent license. Wealthfolio remains separately licensed under [AGPL-3.0](https://github.com/wealthfolio/wealthfolio/blob/d1fda0b65f69d8557b1ef80e555c799215ad28b3/LICENSE). This chart references the unmodified official image; its archive contains no application binary, source, assets or upstream artwork. See [NOTICE](NOTICE) for attribution and scope. Wealthfolio is a trademark of Teymz Inc.; no endorsement is implied.
