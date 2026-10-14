#!/bin/bash

set -eu

SBX_KIT="${HOME}/git/sbx-junie/kit"

CONFIG_HOME="${XDG_CONFIG_HOME-${HOME}/.config}/junie-sbx"
SBX_STATE_DIR="${CONFIG_HOME}/sbx-state"
DOT_JUNIE_RO="${CONFIG_HOME}/junie_ro/_junie"
JUNIE_SHARE="${HOME}/.local/share/junie"

PROJ="$(basename $(pwd))"
JUNIE_HOME="${SBX_STATE_DIR}/${PROJ}"

init_junie_home() {
	mkdir -p "${JUNIE_HOME}"

	# create symlinks
	/usr/bin/ln -s "${DOT_JUNIE_RO}/AGENTS.md" "${JUNIE_HOME}"
	/usr/bin/ln -s "${DOT_JUNIE_RO}/docs" "${JUNIE_HOME}"
	/usr/bin/ln -s "${DOT_JUNIE_RO}/tools" "${JUNIE_HOME}"

	# for now, copy sessions over
	mkdir "${JUNIE_HOME}/sessions"
	cp "${DOT_JUNIE_RO}/sessions/index.jsonl" "${JUNIE_HOME}/sessions/"
	pushd "${JUNIE_HOME}/sessions/" > /dev/null
	find "${DOT_JUNIE_RO}/sessions" -name 'session-*' -exec ln -s {} \;
	popd
}

if [[ ! -d "${JUNIE_HOME}" ]]; then
	read -p "JUNIE_HOME ${JUNIE_HOME} does not exist, create? (y/N): " -n 1 -r
	echo # Moves to a new line
	if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    		echo "Exiting."
    		exit 1
	fi
	
	init_junie_home
fi

sbx run \
    --provider "ollama" --model "deepseek-v4.1-flash:cloud" \
    --env "JUNIE_HOME=${JUNIE_HOME}" --env "USER_HOME=${HOME}" --env "USER_CONFIG_HOME="${CONFIG_HOME} \
    "${SBX_KIT}" \
    . "${JUNIE_HOME}" "${HOME}/.junie/instances:ro" "${DOT_JUNIE_RO}:ro" "${JUNIE_SHARE}:ro" "${HOME}/.local/bin:ro"
