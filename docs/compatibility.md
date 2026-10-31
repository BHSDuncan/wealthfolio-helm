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

K3s clusters use this same kubeconfig-based installation path. Configure a readable kubeconfig for both kubectl and Helm before installation.

## Validation coverage

Static checks cover Helm 3 and 4, invalid values, storage and security settings, and strict Kubernetes 1.34, 1.35 and 1.37 schemas. Installation smoke tests use the official image on kind; local HTTPS and private-CA setup have also been tested. See [CONTRIBUTING](../CONTRIBUTING.md) for reproducible checks.

Other Kubernetes distributions and providers have not been individually verified. Testing does not yet cover every lifecycle operation with the pinned image or live cert-manager/ACME issuance. Future Kubernetes versions also need verification. The pinned image has known [security limitations](security.md#upstream-image) that chart validation cannot resolve.

The default requests of 100m CPU and 256Mi memory, with a 1Gi memory limit, are starting values rather than measured sizing guarantees. Readiness checks do not test profile unlock, persistence or recovery; confirm those by using the application. Recreate updates cause downtime. Forced deletion, network partitions, a second release or overlapping maintenance can still leave multiple writers using the data.

Unsupported features include multiple replicas, rolling updates, HA, PostgreSQL/Redis, arbitrary sidecars, subpath rewrites and automatic database conversion or key rotation. Storage-driver permissions, node-local durability and volume reclaim policies remain operator responsibilities.
