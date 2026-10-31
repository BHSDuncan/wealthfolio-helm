# Chart releases

Chart distribution uses GitHub Container Registry at **`oci://ghcr.io/bhsduncan/charts/wealthfolio`**. Chart versions follow SemVer independently of the Wealthfolio application version. Installation instructions are in the [Helm quick start](../README.md) and [installation guide](install.md); release changes are recorded in the [changelog](../CHANGELOG.md).

Public chart downloads require no account, token or registry login. Helm download and installation commands use the full chart address and an explicit `--version`. Publication targets `oci://ghcr.io/bhsduncan/charts`; Helm appends the chart name and the version from `Chart.yaml`.

## Release artifacts and versioning

Every published chart version is immutable. Chart-only changes receive a new chart version, just as application updates do. Application updates also require review of upstream migrations, image digest/platforms, compatibility and security. Chart metadata, values, schema, examples, tests and documentation describe the same reviewed release.

The [contributor checks](../CONTRIBUTING.md) validate the chart and tooling. Affected integration checks and an installation from the packaged chart establish relevant runtime behavior; chart validation alone does not prove login, persistence or recovery.

```bash
scripts/check.sh
scripts/package.sh
```

Packaging derives the archive name and version from `charts/wealthfolio/Chart.yaml`, producing `dist/wealthfolio-VERSION.tgz` and its SHA256 sidecar. The archive contains the chart, standalone README, license and NOTICE. Release assets contain no credentials, recovery material, private reports or application data.

## Publication workflow

The manual **Owner-approved chart publication** workflow publishes an exact 40-character source commit with successful hosted **Chart validation** and a matching chart version. Both the original actor and the actor initiating a rerun must be the repository owner. Pull requests, pushes and tags do not publish automatically. Publication is also gated by the repository's `RELEASE_PUBLICATION_ENABLED` switch and the `chart-publication` environment.

The workflow packages once, verifies the checksum, publishes that artifact, and compares a registry pullback byte-for-byte. An existing OCI version is accepted only when its downloaded bytes match the authorized artifact; a different artifact is refused. Retrying failed jobs preserves the successful packaging job's artifact, whereas a full rerun may produce different archive bytes. Existing chart versions and GitHub releases are never overwritten.

Anonymous chart access is a required publication check. GHCR package visibility is independent of source-repository visibility; public source alone does not make a package public. Registry visibility is managed in package settings, outside the publication workflow.

The optional GitHub release input attaches the chart archive, checksum and compatibility documentation to a versioned release. Its job alone requests `contents: write`; ordinary chart publication requests `contents: read` and `packages: write`, with `actions: read` for the source-CI gate.

## Actions authentication and package access

Actions uses its per-run built-in token, explicitly mapped into the registry step's environment:

```yaml
permissions:
  contents: read
  packages: write
# Registry authentication step:
env:
  GHCR_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

The registry login supplies the token through stdin with shell tracing disabled:

```bash
set +x
printf '%s' "$GHCR_TOKEN" | helm registry login ghcr.io \
  --username "$GITHUB_ACTOR" --password-stdin
```

`GITHUB_ACTOR` is a built-in Actions environment variable; `GITHUB_TOKEN` is not automatically a shell environment variable. The workflow uses the same built-in secret through `GH_TOKEN` for GitHub CLI API/release operations. It never prints a token or passes one as a command-line password and logs out of GHCR on exit. No replacement Actions token or repository secret named `GITHUB_TOKEN` is involved.

Repository/account policy must permit the requested package permissions. For an existing package, this repository needs **Write** access through the package's **Manage Actions access** settings or equivalent inherited access. Linking a package to a repository does not necessarily change permissions granted before that link. These access controls also apply to packages originally uploaded from a local terminal.

## Local terminal authentication

A local terminal does not receive the Actions built-in token. Local registry authentication uses a **personal access token (classic)** with **`write:packages`**, appropriate account/package write access, and SSO authorization where applicable. The token remains separate from Actions authentication; the publication workflow provides the source-CI, artifact and anonymous-access checks described above.

In Bash, a hidden prompt keeps the classic PAT out of shell history and command-line arguments:

```bash
set +x
read -r -p 'GitHub username: ' GHCR_USERNAME
read -r -s -p 'Classic PAT (write:packages): ' GHCR_PAT
printf '\n'
printf '%s' "$GHCR_PAT" | helm registry login ghcr.io \
  --username "$GHCR_USERNAME" --password-stdin
unset GHCR_PAT
helm registry logout ghcr.io
```

Helm stores credentials in its local registry configuration until logout. Public chart downloads do not need this authenticated session.

## Official references

- [Helm OCI registry usage](https://helm.sh/docs/topics/registries/) and [registry login](https://helm.sh/docs/helm/helm_registry_login/) describe packaging, inferred names/versions and stdin authentication.
- [GitHub Container registry authentication and publication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) covers Actions tokens, classic PAT scopes, package visibility and anonymous pulls.
- [GitHub package access and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility) explains package workflow access and permission inheritance.
