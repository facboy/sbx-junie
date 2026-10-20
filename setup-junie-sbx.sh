#!/bin/bash

set -eu

CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}/junie-sbx"
JUNIE_RO="${CONFIG_HOME}/junie_ro"

# `mount -t` filters on the type bindfs registers as, so a view already up is
# left alone. Otherwise bindfs has to be here: the sandbox mounts the view and
# takes AGENTS.md, docs and tools from it, so a host without it gets no working
# Junie home. Checked before the mountpoint is made, and a mount that fails
# takes that mountpoint back, so a failed run leaves nothing for the next one to
# mount as an empty read-only home.
if ! mount -t fuse.bindfs | fgrep -q "${JUNIE_RO}/_junie"; then
  if ! command -v bindfs > /dev/null 2>&1; then
    echo "junie-sbx: bindfs is not installed, so ~/.junie cannot be exposed to" >&2
    echo "  the sandbox read-only. Install it and re-run." >&2
    exit 1
  fi
  mkdir -p "${JUNIE_RO}/_junie"
  if ! bindfs -r --no-allow-other --resolve-symlinks ~/.junie "${JUNIE_RO}/_junie"; then
    rmdir "${JUNIE_RO}/_junie" 2> /dev/null || true
    echo "junie-sbx: bindfs could not mount ~/.junie at ${JUNIE_RO}/_junie" >&2
    exit 1
  fi
fi

mkdir -p "${CONFIG_HOME}/sbx-state"

# The per-project relay secrets `junie-sbx.sh` writes and the host relay daemon
# reads. Only the owner may read them: a secret is what authorises a sandbox to
# reach its own project's IDE, and every other local process can dial the relay
# port. The daemon refuses to start while either level is wider than this.
mkdir -p -m 700 "${CONFIG_HOME}/relay-secrets"
chmod 700 "${CONFIG_HOME}/relay-secrets"

# The per-project overlays that give the sandbox a writable uv cache and pnpm
# store over your read-only host ones are mounted at every run, by either
# fuse-overlayfs or sudo. Report it here, with the other prerequisites.
if ! command -v fuse-overlayfs > /dev/null 2>&1 && ! sudo -n true 2> /dev/null; then
  echo "junie-sbx: neither fuse-overlayfs nor passwordless sudo is available," >&2
  echo "  so the sandbox will not reuse your host uv cache or pnpm store." >&2
fi
