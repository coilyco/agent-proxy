#!/usr/bin/env bash
# Build and publish the immutable agent-proxy image to GHCR (agent-proxy#97).
# Tag is the full commit sha, which is what coilyco/deploy resolves and pulls.
# See docs/image-publishing.md.
set -euo pipefail

registry="ghcr.io"
image_name="coilyco/agent-proxy"
source_url="https://github.com/coilyco/agent-proxy"

sha="${GITHUB_SHA:-$(git rev-parse HEAD)}"
case "${sha}" in
  *[!0-9a-f]*|"")
    echo "agent-proxy source sha is not a lowercase hexadecimal commit id." >&2
    exit 1
    ;;
esac
if [ "${#sha}" -ne 40 ]; then
  echo "agent-proxy source sha must be a full 40-character commit id." >&2
  exit 1
fi

if [ -z "${REGISTRY_TOKEN:-}" ] || [ -z "${REGISTRY_USER:-}" ]; then
  echo "REGISTRY_USER and REGISTRY_TOKEN are required for the image-publish lane." >&2
  exit 1
fi

image="${registry}/${image_name}:${sha}"

docker_config="$(mktemp -d)"
trap 'rm -rf "${docker_config}"' EXIT
chmod 700 "${docker_config}"
export DOCKER_CONFIG="${docker_config}"

printf '%s' "${REGISTRY_TOKEN}" \
  | docker login "${registry}" --username "${REGISTRY_USER}" --password-stdin

echo "==> building ${image_name}:${sha}"
# The source label is what links the package to this repo on GHCR.
docker build --pull --label "org.opencontainers.image.source=${source_url}" -t "${image}" .

echo "==> publishing ${image_name}:${sha}"
docker push "${image}"

echo "published ${image_name}:${sha}"
