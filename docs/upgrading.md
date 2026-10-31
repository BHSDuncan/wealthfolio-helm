# Upgrades, rollback, and retained storage

Chart and app versions are independent. Before every app change review upstream release notes/migrations, image source/digest/platforms, support/security and license. Back up the **entire stopped data directory**, retain its original key, verify recovery, and ensure space for multiple database copies. Update image tag and digest together; retaining the shipped digest while changing its tag fails validation. Never assume image downgrade reverses database migrations.

## Change access on a managed installation

For an existing installer-managed HTTP installation, opt in to HTTPS explicitly:

```bash
./scripts/install.sh --configure-access
```

The installer reuses the saved hostname and asks for HTTP/HTTPS and the certificate path/inputs when needed. Review the old/new URLs and confirm the change before it is applied. Supply `--scheme https --tls-mode MODE` and its inputs to preselect them; see [HTTPS setup](https.md). Use the same target, namespace, release and state-directory options as the original installation. The default target is your current context from the configured kubeconfig. Keep any original `--context`, `--kubeconfig`, `--ssh` and remote `--sudo` options so the same execution account and cluster are selected. Ordinary reruns do not ask again or alter the saved network settings.

This path verifies the managed release, Secret and claim identities and applies only network settings, preserving the image, master key, data claim and database-encryption policy. It refuses unmanaged or drifted installations. Expect Recreate downtime. DNS and the selected certificate path must match the hostname. No application data reset or master-key rotation occurs; private-CA/cert-manager modes create only their explicitly selected certificate resources. After switching to HTTPS, sign out and back in at the HTTPS URL to replace any earlier HTTP session cookie.

## Chart and application upgrades

For chart-only changes retain image, key, paths, selector and claim. `helm upgrade` uses Recreate: expect downtime and verify old-before-new lifecycle, auth and profile/vault data after readiness. That ordering applies to Deployment upgrades; manually deleting a Pod can create a replacement before the old Pod finishes terminating. For maintenance, scale the Deployment to zero and wait until all pods using the claim have terminated. Same-image `helm rollback` can restore safe chart values, but restores neither the database nor a prior master key. Avoid automatic rollback around app migrations.

A bound PVC's storage class/access modes cannot be changed in place; do not shrink it. With `existingClaim`, its provisioning settings are external. RWO does not prohibit same-node concurrent writers. Never run two releases, a backup writer or an ad-hoc pod against active application data. Forced deletion/partition scenarios need operator judgment beyond Recreate's controlled update guarantee.

`helm uninstall` retains chart-created claims annotated `helm.sh/resource-policy: keep`. Retention is not backup. Namespace deletion removes PVCs; underlying reclaim policy can destroy data; local-path storage remains node-local. Keep the namespace, external Secret and original key.

Before reinstall inspect the retained claim name/UID and namespace without displaying Secret values. Reuse the original configuration and verify independent key identity privately. Explicitly set the retained claim:

```bash
helm install wealthfolio ./charts/wealthfolio \
  --kube-context "$WF_CONTEXT" --namespace "$WF_NAMESPACE" \
  --values "$WF_PRIVATE_VALUES" --set persistence.existingClaim="$WF_RETAINED_CLAIM" \
  --wait --timeout 10m
```

The chart then omits PVC creation, avoiding orphan adoption assumptions; old Helm labels may remain. Reusing a different release name does not authorize manually rewriting claim ownership. Log in and read records from multiple profiles plus vault data after reinstall. Keep new and old installations from overlapping writers.
