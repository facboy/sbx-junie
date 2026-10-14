#!/bin/bash

SBX_KIT="/home/chrisng/git/sbx-kits-contrib/junie"
SBX_STATE_DIR="${HOME}/junie/sbx-state"

PROJ="$(basename $(pwd))"

JUNIE_HOME="${SBX_STATE_DIR}/${PROJ}"

if [[ ! -d "${JUNIE_HOME}" ]]; then
	>&2 echo "JUNIE_HOME ${JUNIE_HOME} does not exist"
	exit 1
fi

sbx run --env JUNIE_HOME=${JUNIE_HOME} "${SBX_KIT}" . "${HOME}/.junie/instances:ro" "${JUNIE_HOME}"
