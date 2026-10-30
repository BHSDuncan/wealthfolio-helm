# Compatibility

| Component | Configuration |
|---|---|
| Chart | 0.1.0; chart and application versions are independent |
| Application | Wealthfolio 3.11.0, unchanged official binary and frontend |
| Kubernetes | 1.34 or later, using stable APIs; one replica only |
| Helm | Tested with 3.22.1 and 4.3.1 |
| Architectures | Linux AMD64 and ARM64; not every CPU model is verified |
| Storage | Durable filesystem PVC, writable by UID/GID 1000; complete `/data` |
| Image | Official `ghcr.io/wealthfolio/wealthfolio:3.11.0`, pinned by digest |

## Validation coverage

Static checks cover Helm 3 and 4, invalid values, storage/security settings and strict Kubernetes 1.34/1.35/1.37 schemas. Official-image installation smoke tests on Kind and local HTTPS/private-CA checks have been exercised. The chart uses standard Kubernetes resources; other distributions and providers have not been individually verified. Complete lifecycle coverage for the pinned official image and live cert-manager/ACME issuance have not been established. See [CONTRIBUTING](../CONTRIBUTING.md) for reproducible checks, and [image security](security.md#upstream-image) for the pinned image's limitations. Future Kubernetes versions require verification.

Default requests of 100m CPU/256Mi and a 1Gi memory limit are starting values, not measured sizing guarantees. Probes are shallow; readiness does not prove profile unlock, persistence or recovery. Recreate updates cause downtime. Forced deletion, partitions, a second release and overlapping maintenance can still create unsafe concurrent writers.

Unsupported features include multiple replicas, rolling updates, HA, PostgreSQL/Redis, arbitrary sidecars, subpath rewrites and automatic database conversion or key rotation. Storage-driver permissions, node-local durability and volume reclaim policies remain operator responsibilities.
