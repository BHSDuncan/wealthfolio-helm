# Releasing the chart

This guide is for chart maintainers. Installation instructions are in the [README](../README.md) and [installation guide](install.md).

## Version and package

Use chart SemVer independently of Wealthfolio's version. Never reuse a published chart version. For application updates, review upstream migrations, image digest/platforms and security, then update chart metadata, values, schema, examples, tests and documentation together. Describe user-visible changes and upgrade requirements in [CHANGELOG](../CHANGELOG.md).

Run the [contributor checks](../CONTRIBUTING.md), affected integration tests and an installation from the packaged chart. Verify login and persistent data for runtime changes; chart validation alone does not establish application compatibility.

```bash
scripts/check.sh
scripts/package.sh
```

Packaging creates `dist/wealthfolio-VERSION.tgz` and its SHA256 sidecar. The archive contains the chart, standalone README, license and NOTICE. Keep credentials, local reports, recovery material and application data out of release assets.

## Publish

The manual publication workflow takes an exact source commit with successful CI, the chart version and an optional GitHub release choice. It requires the `RELEASE_PUBLICATION_ENABLED=true` repository variable and the `chart-publication` environment. Configure environment protection and GHCR package visibility for the repository before publishing.

The workflow verifies the package checksum, publishes to `oci://ghcr.io/REPOSITORY_OWNER/charts/wealthfolio`, checks that the result can be pulled anonymously, and optionally attaches the package and checksum to a GitHub release. Existing OCI versions and GitHub releases are not overwritten. Pull requests, pushes and tags do not publish automatically.

If an initial GHCR upload is private, make the package public and select **Re-run failed jobs** to reuse the successful packaging job's artifact. An existing OCI version is accepted on retry only when its downloaded bytes match that artifact; a full rerun may create different archive bytes and be refused.

After publication, add the verified OCI installation command to the README. Until a registry is actually available, users should install from a source checkout or downloaded chart archive.
