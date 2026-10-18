#!/bin/bash

set -eu

SBX_KIT="${HOME}/git/sbx-junie/kit"

CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}/junie-sbx"
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
USER_PW_CACHE="${HOME}/.cache/ms-playwright"

PROJ="$(basename "$(pwd)")"

# One directory per project holds everything that project's sandbox leaves behind:
# the Junie home it writes to, and the overlays that layer its uv and pnpm writes
# over the host's read-only trees. Overlays are a sibling of the Junie home, not
# inside it -- the Junie home is mounted into the sandbox, and a mount nested
# under it would both be unreliable and expose the writable layer to the agent.
PROJ_DIR="${SBX_STATE_DIR}/${PROJ}"
JUNIE_HOME="${PROJ_DIR}/_junie"
OV_ROOT="${PROJ_DIR}/overlays"

# Per-project switches: which of the host's tool trees this project's sandbox
# gets. They come from `.junie/sbx.toml` in the project directory, as `uv` and
# `pnpm` booleans, and default to on, so a project with no file behaves as it did
# before the switches existed. `JUNIE_SBX_UV` and `JUNIE_SBX_PNPM` override the
# file for a single run. Node and the Playwright browsers follow pnpm: the tree
# pnpm runs on and the browsers it drives go with it.
SWITCH_FILE="${PWD}/.junie/sbx.toml"
UV_ON=1
PNPM_ON=1

switch_value() {
	case "$1" in
		1 | true | yes | on) echo 1 ;;
		0 | false | no | off) echo 0 ;;
		*)
			echo "junie-sbx: $2 wants true or false, not '$1'" >&2
			exit 1
			;;
	esac
}

# tomllib is the stdlib reader the relay daemon also uses, so a file that will
# not parse stops the run rather than leaving a switch looking set when it never
# loaded.
read_switch_file() {
	python3 - "${SWITCH_FILE}" <<-'PY'
		import sys, tomllib

		try:
		    with open(sys.argv[1], "rb") as handle:
		        document = tomllib.load(handle)
		except (OSError, tomllib.TOMLDecodeError) as err:
		    sys.stderr.write("junie-sbx: %s: %s\n" % (sys.argv[1], err))
		    raise SystemExit(1)
		for name in ("uv", "pnpm"):
		    if name not in document:
		        continue
		    value = document[name]
		    if not isinstance(value, bool):
		        sys.stderr.write("junie-sbx: %s wants true or false, not %r\n"
		                         % (name, value))
		        raise SystemExit(1)
		    print("%s=%d" % (name, value))
	PY
}

if [[ -f "${SWITCH_FILE}" ]]; then
	switches="$(read_switch_file)"
	while IFS='=' read -r name value; do
		case "${name}" in
			uv) UV_ON="$(switch_value "${value}" "${SWITCH_FILE}")" ;;
			pnpm) PNPM_ON="$(switch_value "${value}" "${SWITCH_FILE}")" ;;
		esac
	done <<< "${switches}"
fi

if [[ -n "${JUNIE_SBX_UV:-}" ]]; then
	UV_ON="$(switch_value "${JUNIE_SBX_UV}" "JUNIE_SBX_UV")"
fi
if [[ -n "${JUNIE_SBX_PNPM:-}" ]]; then
	PNPM_ON="$(switch_value "${JUNIE_SBX_PNPM}" "JUNIE_SBX_PNPM")"
fi

# One secret per project, provisioned here and handed to the sandbox: the host
# relay daemon splices a connection only when its preamble carries the secret
# recorded for the project the digest resolves to. Loopback is shared with every
# other process on the host -- other users, other sandboxes -- so without it any
# caller could name another project's digest and reach that project's IDE.
SECRETS_DIR="${CONFIG_HOME}/relay-secrets"
provision_relay_secret() {
	umask 077
	mkdir -p "${SECRETS_DIR}"
	# The daemon refuses to start on a secret another local user can reach, so
	# both levels are tightened on every run: a directory an earlier run or a
	# different umask left wider would otherwise stop the relay.
	chmod 700 "${SECRETS_DIR}"
	python3 - "${SECRETS_DIR}" "${PWD}" <<-'PY'
		import hashlib, json, os, sys

		secrets_dir, project = sys.argv[1], sys.argv[2]
		path = os.path.join(secrets_dir, hashlib.md5(project.encode()).hexdigest() + ".json")

		# Kept across runs: a new secret would leave a running sandbox relaying
		# nothing until it was restarted.
		if os.path.exists(path):
		    os.chmod(path, 0o600)
		    with open(path) as handle:
		        print(json.load(handle)["secret"])
		    raise SystemExit

		secret = os.urandom(32).hex()
		with open(path, "w") as handle:
		    json.dump({"project_path": project, "secret": secret}, handle)
		os.chmod(path, 0o600)
		print(secret)
	PY
}
RELAY_SECRET="$(provision_relay_secret)"

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

# Only a tree its switch left on is overlaid and mounted; an off tree is absent
# from the sandbox entirely, so its cache and store stay the host's alone.
UV_CACHE_MERGED=""
UV_TOOLS_MERGED=""
PNPM_CACHE_MERGED=""
PNPM_MERGED=""
PW_CACHE_MERGED=""

if [[ "${UV_ON}" == 1 ]]; then
	merge_overlay "${USER_UV_CACHE}" uv-cache
	UV_CACHE_MERGED="${MERGED_PATH}"
	merge_overlay "${USER_UV}/tools" uv-tools
	UV_TOOLS_MERGED="${MERGED_PATH}"
fi
if [[ "${PNPM_ON}" == 1 ]]; then
	merge_overlay "${USER_PNPM_CACHE}" pnpm-cache
	PNPM_CACHE_MERGED="${MERGED_PATH}"
	merge_overlay "${USER_PNPM}" pnpm
	PNPM_MERGED="${MERGED_PATH}"
	merge_overlay "${USER_PW_CACHE}" ms-playwright
	PW_CACHE_MERGED="${MERGED_PATH}"
fi

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

if [[ "${UV_ON}" == 1 ]]; then
	add_mount "${USER_UV}" ":ro"
	add_mount "${UV_CACHE_MERGED}" ""
	add_mount "${UV_TOOLS_MERGED}" ""
fi
if [[ "${PNPM_ON}" == 1 ]]; then
	add_mount "${USER_FNM}" ":ro"
	add_mount "${PNPM_CACHE_MERGED}" ""
	add_mount "${PNPM_MERGED}" ""
	add_mount "${PW_CACHE_MERGED}" ""
fi

# Only the variables a mounted tree needs reach the sandbox: with a switch off
# its paths are absent, which is what leaves `junie-init.sh` linking nothing from
# it. The resolved switches themselves always go, so the startup script knows
# which trees the sandbox was given.
ENVS=(
	--env "JUNIE_HOME=${JUNIE_HOME}"
	--env "USER_HOME=${HOME}"
	--env "USER_CONFIG_HOME=${CONFIG_HOME}"
	--env "JUNIE_IDE_RELAY_SECRET=${RELAY_SECRET}"
	--env "JUNIE_SBX_UV=${UV_ON}"
	--env "JUNIE_SBX_PNPM=${PNPM_ON}"
)
if [[ "${UV_ON}" == 1 ]]; then
	ENVS+=(
		--env "UV_PYTHON_INSTALL_DIR=${USER_UV}/python"
		--env "UV_TOOL_DIR=${UV_TOOLS_MERGED}"
		--env "UV_CACHE_DIR=${UV_CACHE_MERGED}"
	)
fi
if [[ "${PNPM_ON}" == 1 ]]; then
	ENVS+=(
		--env "PNPM_HOME=${PNPM_MERGED}"
		--env "JUNIE_PNPM_STORE=${PNPM_MERGED}/store"
		--env "JUNIE_PNPM_CACHE=${PNPM_CACHE_MERGED}"
		--env "PLAYWRIGHT_BROWSERS_PATH=${PW_CACHE_MERGED}"
	)
fi

exec sbx run \
    --provider "ollama" --model "deepseek-v4.1-flash:cloud" \
    "${ENVS[@]}" \
    "${SBX_KIT}" \
    "${MOUNTS[@]}"
