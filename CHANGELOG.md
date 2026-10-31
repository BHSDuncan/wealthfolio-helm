# Changelog

## 0.1.0 — unreleased

- Initial community chart for Wealthfolio 3.11.0, using the official image pinned by digest.
- Single non-root application instance with retained persistent storage and external authentication credentials.
- Helm-first installation with standalone credential preparation, nonsecret values files and optional guided setup.
- Guided application installation and certificate setup default to the current Kubernetes context, with a shared kubeconfig for kubectl/Helm and optional SSH execution.
- Optional HTTPS, private localhost forwarding and authenticated MCP access, strict values validation, and chart/installer integration checks.
