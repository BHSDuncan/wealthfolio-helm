# Backup and recovery

Back up the entire `/data` directory. It contains the registry and its backup, every profile database and SQLite sidecar file, the vault, application-managed backups, addons and hidden files. A copy of one database cannot recover the complete installation.

Keep the **original master key in a separate protected backup**, along with the nonsecret deployment configuration, image version/digest and database-encryption setting. Vault encryption does not encrypt plaintext databases or backup archives. Store archives outside the repository with restricted access and encryption, and verify both their checksums and a recovery test.

## Take a stopped backup

Run the repository helpers from the checkout root with Python 3 and kubectl available. Set the following variables for the installation you intend to back up:

| Variable | Meaning |
|---|---|
| `WF_KUBECONFIG`, `WF_CONTEXT` | Readable kubeconfig file and verified context |
| `WF_NAMESPACE`, `WF_CLAIM` | Application namespace and data PVC name |
| `WF_CLUSTER_UID` | UID of that cluster's `kube-system` namespace, used as an identity check |
| `WF_PRIVATE_BACKUP` | New archive path outside the repository; the helper refuses to overwrite a file |

Inspect the selected context and resources before stopping the application. You can read the cluster UID with `kubectl --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" get namespace kube-system -o jsonpath='{.metadata.uid}'`. The commands below assume the Deployment and release are named `wealthfolio`; replace those names if yours differ.

Stop the Deployment and wait until all application pods using the claim are gone. Only one writer may use the data at a time: Recreate and ReadWriteOnce storage do not make overlapping maintenance pods safe. The backup helper checks the target identity and refuses a claim still mounted by a pod.

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

The helper writes a private archive and a `.sha256` file, then removes its maintenance pod. It uses a digest-pinned utility image, UID/GID 1000, no service-account token or extra privileges, and a read-only mount of `/data`. The archive includes hidden files and SQLite sidecars.

The application stays stopped when the helper finishes. Confirm that the maintenance pod is gone before scaling the Deployment back to one. If a command fails, inspect the reported state first and keep diagnostics private. Do not use a partial archive or copy a live SQLite database as a backup.

## Test a restore on separate storage

The supplied restore helper supports **empty synthetic test claims only**. It cannot overwrite an existing installation's valuable data. Prepare a separate test target with:

- A new filesystem PVC writable by UID/GID 1000, with no pod mounting it.
- The same nonempty `wealthfolio.github.io/test-run` label on the test namespace and PVC.
- Exactly one Deployment selected by the test release's `app.kubernetes.io/instance` label, scaled to zero.

These labels identify disposable test resources; do not add them to a live installation to bypass the restriction. Set `WF_TEST_NAMESPACE`, `WF_TEST_RELEASE` and `WF_NEW_CLAIM` to that test target. If it is on another cluster, select that cluster's `WF_KUBECONFIG` and `WF_CONTEXT` and verify its `WF_CLUSTER_UID`. Set `WF_BACKUP_SHA256` to the digest in the backup's verified `.sha256` file:

```bash
scripts/restore.sh --kubeconfig "$WF_KUBECONFIG" --context "$WF_CONTEXT" \
  --namespace "$WF_TEST_NAMESPACE" --release "$WF_TEST_RELEASE" --claim "$WF_NEW_CLAIM" \
  --confirm-cluster-uid "$WF_CLUSTER_UID" --confirm-stopped \
  --input "$WF_PRIVATE_BACKUP" --sha256 "$WF_BACKUP_SHA256" \
  --confirm-empty-test-claim
```

The helper verifies the checksum and archive structure, checks that the claim is empty, restores it, and leaves the application stopped. Before starting the sole application pod, confirm that the maintenance pod is gone. Configure the restored claim with `persistence.existingClaim`, a Secret containing the original master key, the same image and the matching encryption setting.

Log in and read records from multiple profiles, check saved vault data, and review audit and token state. A sentinel file or ready probe alone does not establish successful recovery. Review and revoke personal access tokens that an older backup may have restored.

## Recover an installation

For recovery outside the synthetic test environment, plan a stopped, offline restore with a verified pre-restore backup and the original master key. Validate the restored data on new storage before replacing the installation, and keep every other writer stopped. The supplied helper does not perform the replacement. Deleting a namespace can destroy retained claims; remove only the test resources you intend to discard.

Portable exports and application-managed snapshots complement a whole-directory archive, but do not capture the complete installation. A portable-export password is separate from the master key; encrypted raw snapshots still require their original key. This recovery procedure does not use a live web restore endpoint.

Database encryption or decryption must run offline for every profile, with the same key, image and operating-system user, a complete pre-operation backup and enough free space. Changing `encryption.required` alone does not convert files; see [database encryption](security.md#database-encryption).
