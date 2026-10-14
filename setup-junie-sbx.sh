#!/bin/bash

set -eu

CONFIG_HOME="${XDG_CONFIG_HOME-${HOME}/.config}/junie-sbx"
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
