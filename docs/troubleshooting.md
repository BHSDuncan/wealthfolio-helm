# Troubleshooting

Start with the affected resources and recent events. Set `WF_CONTEXT` and `WF_NAMESPACE` to the installation's verified context and namespace. Use the pod name from the first command as `WF_POD` for the final command:

```bash
kubectl --context "$WF_CONTEXT" -n "$WF_NAMESPACE" get pods,pvc,svc
kubectl --context "$WF_CONTEXT" -n "$WF_NAMESPACE" get events --sort-by=.lastTimestamp
kubectl --context "$WF_CONTEXT" -n "$WF_NAMESPACE" describe pod "$WF_POD"
```

Inspect conditions and events before collecting application logs. When sharing a report, redact private identifiers and exclude Secret YAML, kubeconfigs, raw cookies, token responses and full browser traces.

| Symptom | Check |
|---|---|
| Helm template/lint rejects the auth reference | Set `auth.existingSecret` in a nonsecret values file; even a local preview needs the Secret's name |
| CreateContainerConfigError | Check that the named Secret and both required keys exist in the release namespace, without printing their values |
| Application configuration failure | Check that the master key decodes to 32 bytes and the password hash is valid Argon2id PHC text with single `$` characters. Inspect logs privately for the error category |
| Hostname does not reach the app | DNS points to the ingress controller's address, the selected ingress class exists, and the network permits the controller's ports |
| TLS warning | Check the certificate selected by your controller or TLS Secret. It must cover the browser hostname and chain to a CA the browser trusts; a referenced Secret must be in the release namespace |
| Local access is refused | For Helm installs, use the documented kubectl port-forward with the configured exact origin. The optional installer access helper reconnects only releases created with `--local-access`; it does not rewrite origins or Secure-cookie settings |
| Login fails or repeats the login page | Check the password, five-attempt-per-minute-per-IP rate limit, exact scheme/host/port in CORS, forwarded protocol and Secure-cookie setting |
| Empty or slow startup | Check migration progress, free space for backups and the startup-probe window. Readiness does not test login or recovery of saved data |
| PVC Pending | Check the StorageClass/default provisioner, requested capacity and access mode, and scheduling constraints. Do not change the class of a bound claim |
| Permission denied | Check UID/GID 1000, driver support for `fsGroup`, and existing ownership/access-control lists. Stop and back up before an offline repair |
| Multi-attach or database-lock errors | Look for an old pod still terminating, a maintenance pod or a second release using the claim. Do not force concurrent access |
| Missing profiles or vault decryption failure | Check the complete `/data` registry and profile directories, original master key and encryption setting. Generating a new key will not recover the data |
| Encrypted/plaintext format mismatch | The encryption flag does not convert databases. Conversion requires stopped writers and backups for every profile |
| Image crash / illegal instruction | Exact digest and node architecture/CPU; a manifest's presence is not proof for every ARM CPU |
| Kubernetes version rejected by Helm | Check the [supported versions](compatibility.md). For an offline preview, supply a supported `--kube-version`; this does not change the real cluster |
| MCP denied | Check that MCP is enabled, the PAT is valid and has the right scopes/profile binding, and the request Host is allowed. Browser session cookies are not MCP tokens |

See [backup/recovery](backup-restore.md) before destructive repairs. Do not bypass TLS, authentication, schema validation or ownership safeguards to get a green test. Report upstream image defects separately from chart configuration and environment failures.
