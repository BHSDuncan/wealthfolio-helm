# Troubleshooting

Use explicit verified context and namespace. Inspect resource conditions/events before logs; redact private identifiers and never attach Secret YAML, kubeconfig, raw cookies, token responses or full browser traces.

```bash
kubectl --context "$WF_CONTEXT" -n "$WF_NAMESPACE" get pods,pvc,svc
kubectl --context "$WF_CONTEXT" -n "$WF_NAMESPACE" get events --sort-by=.lastTimestamp
kubectl --context "$WF_CONTEXT" -n "$WF_NAMESPACE" describe pod "$WF_POD"
```

| Symptom | Check |
|---|---|
| Bare Helm template/lint fails auth reference | Intended: set nonsecret `auth.existingSecret` in a values file |
| CreateContainerConfigError | Existing Secret and both key names exist in the same namespace; do not print values |
| Application config failure | Master key decodes to exactly 32 bytes; valid literal Argon2id PHC hash; no doubled dollars; inspect private logs for category only |
| Hostname does not reach the app | DNS points to the K3s ingress address, the selected ingress class exists, and the network permits the controller's ports |
| TLS warning | Existing TLS Secret is in the release namespace; its certificate covers the hostname and is trusted by the browser |
| Local access is refused | Use the configured Ingress URL, or reconnect a release originally created with `--local-access`; the helper does not rewrite origins or Secure-cookie settings |
| Login fails or cookie loop | Password, five-attempt/minute/IP rate limit, exact scheme/host/port CORS, forwarded protocol and cookie Secure value |
| Empty/slow startup | Migrations and backup space; startup probe window; readiness is shallow, not recovery proof |
| PVC Pending | StorageClass/default provisioner, requested capacity/access mode, scheduler/node constraints; do not change a bound class |
| Permission denied | Actual UID/GID 1000, driver fsGroup support, old ownership/ACLs; stop and back up before approved offline repair |
| Multiattach/lock errors | Another writer, old pod not terminated, maintenance pod, or second release; never force concurrent access |
| Missing profiles/vault decryption | Whole `/data` registry/backup and profile directories, original key, correct encryption flag; do not create a new key as a repair |
| Encrypted/plain mismatch | Flag does not convert databases; follow stopped per-profile conversion with backups |
| Image crash / illegal instruction | Exact digest and node architecture/CPU; a manifest's presence is not proof for every ARM CPU |
| K3s rejected by Helm | Version below declared support floor; do not upgrade the shared cluster automatically |
| MCP denied | Enabled flag, PAT expiry/revocation/scopes/profile binding, explicit Host allowlist; password cookies are not PATs |

See [backup/recovery](backup-restore.md) before destructive repairs. Do not bypass TLS, authentication, schema validation or ownership safeguards to get a green test. Report upstream image defects separately from chart configuration and environment failures.
