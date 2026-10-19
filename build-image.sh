#!/bin/bash

set -eu

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT="${SCRIPT_DIR}/kit"

# The image the spec names, so a plain run rebuilds what the sandbox boots.
# `IMAGE` names another tag, and any argument is passed to `docker build`.
IMAGE="${IMAGE:-docker.io/facboy/junie-sbx:latest}"

docker build --tag "${IMAGE}" "$@" "${KIT}"

echo "built ${IMAGE}"
