# Stopped whole-directory backup and recovery

Back up all `/data`, not one SQLite file: registry and registry backup, every profile database and sidecar, vault, managed backups, addons and hidden files. Retain the **original master key separately**, plus nonsecret deployment configuration and matching image/encryption flag. Vault encryption does not encrypt plaintext databases or the archive. Protect archives outside the repository with restricted access/encryption and verify their checksums and recovery.

Only one writer is permitted. Stop the target Deployment explicitly and wait until all application pods using the claim are gone. Recreate/RWO do not authorize overlapping maintenance pods. The scripts require explicit target identity and refuse active writers; they leave the app stopped. Keep failure diagnostics private and never resume from a partial restore.

```bash
kubectl --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" -n "$WF_NAMESPACE" \
  scale deployment/wealthfolio --replicas=0
kubectl --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" -n "$WF_NAMESPACE" \
  wait --for=delete pod -l app.kubernetes.io/instance=wealthfolio --timeout=120s
scripts/backup.sh --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" \
  --namespace "$WF_NAMESPACE" --release wealthfolio --claim "$WF_CLAIM" \
  --confirm-cluster-uid "$WF_CLUSTER_UID" --confirm-stopped \
  --output "$WF_PRIVATE_BACKUP"
```

Substitute rendered Deployment/release names, and confirm no other pod mounts the claim. The maintenance pod uses a digest-pinned small utility image, UID/GID 1000, no service-account token or extra privileges, and read-only `/data` for backup. It archives `.` including hidden files and sidecars. Confirm maintenance pod removal before scaling back to one; on failure inspect the reported stopped state first. Never use a backup job concurrent with the live application or a raw live SQLite copy.

## Restore to a new synthetic claim first

The supplied restore script deliberately supports **empty marked synthetic test claims**, not overwriting valuable data. Use a new namespace-local filesystem claim, matching test-run ownership labels and UID/GID 1000 permissions. Confirm its emptiness and no active writer. Use the archive's verified SHA256:

```bash
scripts/restore.sh --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" \
  --namespace "$WF_TEST_NAMESPACE" --release "$WF_TEST_RELEASE" --claim "$WF_NEW_CLAIM" \
  --confirm-cluster-uid "$WF_CLUSTER_UID" --confirm-stopped \
  --input "$WF_PRIVATE_BACKUP" --sha256 "$WF_BACKUP_SHA256" \
  --confirm-empty-test-claim
```

Mount the restored claim using `persistence.existingClaim`, original Secret/key, same image and matching encryption flag. Remove maintenance pods before starting the sole application. Log in and verify actual records in multiple profiles, synthetic saved vault data and audit/token state. A sentinel file or ready probe alone is insufficient. Review/revoke PATs that an old backup resurrects.

The restore script cannot overwrite production data. For production recovery, plan a stopped, offline restore with a verified pre-restore backup and the original master key. Validate recovery on new storage before replacing an installation. Deleting a namespace can destroy retained claims; remove only the test resources you intend to discard.

Portable exports and application-managed snapshots complement this archive but do not capture the complete installation. A portable-export password is distinct from the master key; encrypted raw snapshots retain their original key requirement. No live web restore endpoint is assumed. Database encrypt/decrypt must run offline per profile with the same key/image/user, complete pre-operation backup and adequate free space; merely changing the chart flag fails closed.
