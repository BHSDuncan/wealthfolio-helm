# Upgrades, rollback, and retained storage

Chart and application versions are independent. Before an application update, review upstream release notes and migrations, the image source and supported platforms, security support and licensing. Back up the **entire stopped data directory**, retain its original master key, test recovery, and leave enough space for multiple database copies. See [backup and recovery](backup-restore.md) for the stopped-backup procedure.

Update the image tag and digest together; changing the shipped tag while retaining its old digest fails validation. Downgrading an image does not reverse database migrations.

## Chart and application upgrades

For a chart-only update, retain the image, master key, data paths, Deployment selector and claim. Use your reviewed nonsecret values file with `helm upgrade`, an explicit chart version and the intended context and namespace. The Deployment uses Recreate, so upgrades cause downtime while the old pod stops and its replacement starts. After readiness, confirm login and read actual profile and vault data.

Recreate's ordering applies to Deployment updates. Manually deleting a pod can start its replacement before the old pod finishes terminating. For maintenance, scale the Deployment to zero and wait until all pods using the claim have terminated. A same-image `helm rollback` can restore suitable chart values, but it restores neither database contents nor a previous master key. Avoid automatic rollback around application migrations.

## Storage retention and reinstallation

A bound PVC's storage class and access modes cannot be changed in place; do not shrink it. An externally supplied `persistence.existingClaim` keeps its own provisioning settings. ReadWriteOnce does not prohibit concurrent writers on the same node. Never run two releases or an overlapping maintenance pod against active application data. Forced deletion and network partitions require operator judgment beyond Recreate's normal update behavior.

`helm uninstall` retains chart-created claims annotated `helm.sh/resource-policy: keep`. Retention is not backup. Namespace deletion removes PVCs; underlying reclaim policy can destroy data; local-path storage remains node-local. Keep the namespace, external Secret and original key.

Before reinstalling, inspect the retained claim's name, UID and namespace without displaying Secret values. Reuse the original configuration and privately verify the original key against your independent backup. In this example, set `WF_CONTEXT` and `WF_NAMESPACE` to the verified target, `WF_PRIVATE_VALUES` to your original nonsecret values file, and `WF_RETAINED_CLAIM` to the retained data PVC. The existing authentication Secret must still be present. Pin the chart version that matches your recovery plan; this example uses `0.1.0`:

```bash
helm install wealthfolio oci://ghcr.io/bhsduncan/charts/wealthfolio \
  --version 0.1.0 \
  --kube-context "$WF_CONTEXT" --namespace "$WF_NAMESPACE" \
  --values "$WF_PRIVATE_VALUES" --set persistence.existingClaim="$WF_RETAINED_CLAIM" \
  --wait --timeout 10m
```

The chart references that claim without trying to create or adopt it; old Helm labels may remain. Do not manually rewrite claim ownership to reuse a different release name. After reinstalling, log in and read records from multiple profiles and the vault. Keep the old installation stopped so there is only one writer.

## Change access on an installer-managed installation

For an existing installer-managed HTTP installation, opt in to HTTPS explicitly:

```bash
./scripts/install.sh --configure-access
```

The installer reuses the saved hostname and asks for HTTP/HTTPS and the certificate path/inputs when needed. Review the old/new URLs and confirm the change before it is applied. Supply `--scheme https --tls-mode MODE` and its inputs to preselect them; see [HTTPS setup](https.md). Use the same target, namespace, release and state-directory options as the original installation. The default target is your current context from the configured kubeconfig. Keep any original `--context`, `--kubeconfig`, `--ssh` and remote `--sudo` options so the same execution account and cluster are selected. Ordinary reruns do not ask again or alter the saved network settings.

This path verifies the managed release, Secret and claim identities and applies only network settings, preserving the image, master key, data claim and database-encryption policy. It refuses unmanaged or drifted installations. Expect Recreate downtime. DNS and the selected certificate path must match the hostname. No application data reset or master-key rotation occurs; private-CA/cert-manager modes create only their explicitly selected certificate resources. After switching to HTTPS, sign out and back in at the HTTPS URL to replace any earlier HTTP session cookie.
