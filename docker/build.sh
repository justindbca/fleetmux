#!/bin/bash
# Build the fleetmux image from one commit (docker/Dockerfile).
#   docker/build.sh [REF] [docker buildx build args...]
# The build context is `git archive REF`, so uncommitted changes never reach an image. Tags: <repo>:<12-char sha>
# and <repo>:latest, with <repo> = $FLEETMUX_IMAGE_REPO, else fleetmux. Extra args go to `docker buildx build`,
# e.g. a multi-arch push to a registry:
#   FLEETMUX_IMAGE_REPO=ghcr.io/<owner>/fleetmux docker/build.sh HEAD --platform linux/amd64,linux/arm64 --push
# Needs Docker with buildx (Debian / Ubuntu: the docker-buildx package).
set -euo pipefail
cd "$(dirname "$0")/.."
ref=HEAD
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then ref=$1; shift; fi
sha=$(git rev-parse --verify "$ref^{commit}")
short=${sha:0:12}
repo=${FLEETMUX_IMAGE_REPO:-fleetmux}
if [ "$ref" = HEAD ] && ! git diff --quiet HEAD; then
  echo "note: uncommitted changes are not in the image (it is built from $short)" >&2
fi
git archive --format=tar "$sha" \
  | docker buildx build -f docker/Dockerfile --build-arg FLEETMUX_SHA="$sha" \
      -t "$repo:$short" -t "$repo:latest" "$@" -
echo "built $repo:$short (also $repo:latest)"
