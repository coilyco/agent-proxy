# Image publishing

How the agent-proxy container image reaches GHCR.

## Lane

[`.github/workflows/build-publish.yml`](../.github/workflows/build-publish.yml)
runs on every push to `main` and on manual dispatch, on GitHub-hosted runners.
This repo is public, so no job here uses the private `kai-server` runner. It has
two jobs:

1. **gate** - the same checks `ci.yml` runs: pytest, ruff, black, mypy, the
   daemonless boot probe, and the import smoke.
2. **publish** - holds `packages: write`, and calls
   [`scripts/publish-image.sh`](../scripts/publish-image.sh).

The gate is repeated inside this workflow on purpose. Separate workflows do not
wait for one another, so a publish job that depended on `ci.yml` would race it
and could push an image whose tests never passed.

## Tag

The image is `ghcr.io/coilyco/agent-proxy:<sha>`, where `<sha>` is the full
40-character commit id. The script rejects anything that is not lowercase
hexadecimal of exactly that length, so a short sha or a branch name can never
become a tag. The build carries the `org.opencontainers.image.source` label,
which is what links the package to this repository on GHCR.

Tagging by commit is what makes the image immutable and what `coilyco/deploy`
resolves when it rolls a release out. There is no `latest`. The image was
`forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy:<sha>` until Forgejo was
deleted on 2026-11-01, so a consumer still naming that registry needs repointing.

## Boundary

This repository builds and publishes. It does not deploy, and it holds no cluster
credential. Rollout stays driven from the private `coilyco/deploy` repo per the
source-to-deploy layer invariant.

## Credential

The publish step logs in with the job's own `GITHUB_TOKEN` as `REGISTRY_TOKEN`,
and the actor as `REGISTRY_USER`, so no repository secret carries a registry
credential. The script fails closed when either is absent, logs in through a
private `DOCKER_CONFIG` in a temporary directory, and removes that directory on
exit so no credential survives the job. `SENTRY_CI_DSN` is the one repository
secret, used by the failure alert step.

## See also

- [`proxy.md`](proxy.md) - the request path and how to run the proxy locally.
