# Image publishing

How the agent-proxy container image reached the Forgejo OCI registry.

**Retired with Forgejo on 2026-11-01.** The workflow and the registry no longer
run, and this repo has no GitHub publish workflow or GHCR image. The sections
below record the retired lane. Registry teardown is COI-2931.

## Lane

[`.forgejo/workflows/build-publish.yml`](../.forgejo/workflows/build-publish.yml)
runs on every push to `main` and on manual dispatch. It has two jobs:

1. **gate** - the same checks `ci.yml` runs: pytest, ruff, black, mypy, the
   daemonless boot probe, and the import smoke.
2. **publish** - runs on the `deploy` runner, which has a Docker daemon and the
   registry credential, and calls
   [`scripts/publish-image.sh`](../scripts/publish-image.sh).

The gate is repeated inside this workflow on purpose. Separate workflows do not
wait for one another, so a publish job that depended on `ci.yml` would race it
and could push an image whose tests never passed.

## Tag

The image was `forgejo.coilysiren.me/coilyco-flight-deck/agent-proxy:<sha>`, where
`<sha>` is the full 40-character commit id. The script rejects anything that is
not lowercase hexadecimal of exactly that length, so a short sha or a branch name
can never become a tag.

Tagging by commit is what makes the image immutable and what
`coilyco-bridge/deploy` resolves when it rolls a release out. There is no
`latest`.

## Boundary

This repository builds and publishes. It does not deploy. Rollout stays driven
from `coilyco-bridge/deploy/services/agent-proxy` per the source-to-deploy layer
invariant, and that repository keeps its own publish lane as the rollback path.

## Credential

`REGISTRY_TOKEN` is a repository secret passed only to the publish step. The
script fails closed when it is absent, logs in through a private `DOCKER_CONFIG`
in a temporary directory, and removes that directory on exit so no credential
survives the job.

## See also

- [`proxy.md`](proxy.md) - the request path and how to run the proxy locally.
