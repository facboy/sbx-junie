#!/bin/bash

set -eu

SBX_KIT="${HOME}/git/sbx-junie/kit"

CONFIG_HOME="${XDG_CONFIG_HOME-${HOME}/.config}/junie-sbx"
SBX_STATE_DIR="${CONFIG_HOME}/sbx-state"
DOT_JUNIE_RO="${CONFIG_HOME}/junie_ro/_junie"
JUNIE_SHARE="${HOME}/.local/share/junie"

# Host tool chains. sbx mounts every argument at the host's own path and a
# sandbox cannot mount, so a tree the sandbox has to write to is given an overlay
# built here: the host tree is the read-only lower layer and the sandbox's writes
# land in a per-project upper layer. A venv records its interpreter by absolute
# path and pnpm keys its store to a version and a location, so these are reached
# by environment variable rather than by being moved.
USER_UV="${HOME}/.local/share/uv"
USER_UV_CACHE="${HOME}/.cache/uv"
USER_FNM="${HOME}/.local/share/fnm"
USER_PNPM="${HOME}/.local/share/pnpm"
USER_PNPM_CACHE="${HOME}/.cache/pnpm"

PROJ="$(basename "$(pwd)")"

# One directory per project holds everything that project's sandbox leaves behind:
# the Junie home it writes to, and the overlays that layer its uv and pnpm writes
# over the host's read-only trees. Overlays are a sibling of the Junie home, not
# inside it -- the Junie home is mounted into the sandbox, and a mount nested
# under it would both be unreliable and expose the writable layer to the agent.
PROJ_DIR="${SBX_STATE_DIR}/${PROJ}"
JUNIE_HOME="${PROJ_DIR}/_junie"
OV_ROOT="${PROJ_DIR}/overlays"

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

# Overlays lower onto a per-project upper layer, leaving the merged path in
# MERGED_PATH. Mounting needs CAP_SYS_ADMIN, which the host provides through
# either fuse-overlayfs (FUSE, no root) or passwordless sudo. Failure is not
# fatal: the merged directory is then empty and the sandbox keeps its own cache
# and store there, having reused nothing.
merge_overlay() {
	local lower="$1" base="${OV_ROOT}/$2" opts
	MERGED_PATH="${base}/merged"
	mkdir -p "${base}/upper" "${base}/work" "${MERGED_PATH}"

	[[ -d "${lower}" ]] || return 0
	if grep -qF " ${MERGED_PATH} " /proc/self/mounts; then
		return 0
	fi

	opts="lowerdir=${lower},upperdir=${base}/upper,workdir=${base}/work"
	if command -v fuse-overlayfs > /dev/null 2>&1; then
		fuse-overlayfs -o "${opts}" "${MERGED_PATH}" ||
			echo "junie-sbx: fuse-overlayfs failed for ${lower}" >&2
	elif sudo -n true 2> /dev/null; then
		sudo -n mount -t overlay overlay -o "${opts}" "${MERGED_PATH}" ||
			echo "junie-sbx: mount failed for ${lower}" >&2
	else
		echo "junie-sbx: cannot overlay ${lower}: install fuse-overlayfs or allow passwordless sudo" >&2
	fi
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

merge_overlay "${USER_UV_CACHE}" uv-cache
UV_CACHE_MERGED="${MERGED_PATH}"
merge_overlay "${USER_UV}/tools" uv-tools
UV_TOOLS_MERGED="${MERGED_PATH}"
merge_overlay "${USER_PNPM_CACHE}" pnpm-cache
PNPM_CACHE_MERGED="${MERGED_PATH}"
merge_overlay "${USER_PNPM}" pnpm
PNPM_MERGED="${MERGED_PATH}"

MOUNTS=(
	.
	"${JUNIE_HOME}"
	"${HOME}/.junie/instances:ro"
	"${DOT_JUNIE_RO}:ro"
	"${JUNIE_SHARE}:ro"
	"${HOME}/.local/bin:ro"
)

# sbx rejects a mount whose host path is missing, so the optional host trees are
# added only when present. The overlays were created above and so always exist.
add_mount() {
	if [[ -e "$1" ]]; then
		MOUNTS+=("$1$2")
	fi
}

add_mount "${USER_UV}" ":ro"
add_mount "${USER_FNM}" ":ro"
add_mount "${UV_CACHE_MERGED}" ""
add_mount "${UV_TOOLS_MERGED}" ""
add_mount "${PNPM_CACHE_MERGED}" ""
add_mount "${PNPM_MERGED}" ""

sbx run \
    --provider "ollama" --model "deepseek-v4.1-flash:cloud" \
    --env "JUNIE_HOME=${JUNIE_HOME}" --env "USER_HOME=${HOME}" --env "USER_CONFIG_HOME=${CONFIG_HOME}" \
    --env "UV_PYTHON_INSTALL_DIR=${USER_UV}/python" --env "UV_TOOL_DIR=${UV_TOOLS_MERGED}" \
    --env "UV_CACHE_DIR=${UV_CACHE_MERGED}" \
    --env "PNPM_HOME=${PNPM_MERGED}" \
    --env "JUNIE_PNPM_STORE=${PNPM_MERGED}/store" --env "JUNIE_PNPM_CACHE=${PNPM_CACHE_MERGED}" \
    "${SBX_KIT}" \
    "${MOUNTS[@]}"
