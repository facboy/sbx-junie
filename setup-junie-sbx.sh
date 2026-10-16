#!/bin/bash

set -eu

CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}/junie-sbx"
JUNIE_RO="${CONFIG_HOME}/junie_ro"

mkdir -p "${JUNIE_RO}/_junie"
if ! mount -t fuse.bindfs | fgrep -q "${JUNIE_RO}/_junie"; then
  bindfs -r --no-allow-other --resolve-symlinks ~/.junie "${JUNIE_RO}/_junie"
fi

mkdir -p "${JUNIE_RO}/_local/bin"
if [[ ! -L "${JUNIE_RO}/_local/bin/junie" ]]; then
  ln -s ~/.local/bin/junie "${JUNIE_RO}/_local/bin/"
fi

mkdir -p "${CONFIG_HOME}/sbx-state"

# The per-project overlays that give the sandbox a writable uv cache and pnpm
# store over your read-only host ones are mounted at every run, by either
# fuse-overlayfs or sudo. Report it here, with the other prerequisites.
if ! command -v fuse-overlayfs > /dev/null 2>&1 && ! sudo -n true 2> /dev/null; then
  echo "junie-sbx: neither fuse-overlayfs nor passwordless sudo is available," >&2
  echo "  so the sandbox will not reuse your host uv cache or pnpm store." >&2
fi
