#!/bin/bash

SBX_KIT="/home/chrisng/git/sbx-kits-contrib/junie"
SBX_STATE_DIR="${HOME}/junie/sbx-state"
JUNIE_RO="${HOME}/junie/junie_ro"

PROJ="$(basename $(pwd))"

JUNIE_HOME="${SBX_STATE_DIR}/${PROJ}"

init_junie_home() {
	mkdir -p "${JUNIE_HOME}"

	# create symlinks
	ln -s "${JUNIE_RO}/AGENTS.md" "${JUNIE_HOME}"
	ln -s "${JUNIE_RO}/docs" "${JUNIE_HOME}"
	ln -s "${JUNIE_RO}/tools" "${JUNIE_HOME}"

	# for now, copy sessions over
	mkdir "${JUNIE_HOME}/sessions"
	cp "${JUNIE_RO}/sessions/index.jsonl" "${JUNIE_HOME}/sessions/"
	pushd "${JUNIE_HOME}/sessions/" > /dev/null
	find "${JUNIE_RO}/sessions" -name 'session-*' -exec ln -s {} \;
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

sbx run --provider "ollama" --model "deepseek-v4.1-flash:cloud" --env "JUNIE_HOME=${JUNIE_HOME}" "${SBX_KIT}" . "${HOME}/.junie/instances:ro" "${JUNIE_HOME}" "${JUNIE_RO}:ro"
