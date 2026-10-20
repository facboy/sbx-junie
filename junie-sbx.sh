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

# Per-project settings: which of the host's tool trees this project's sandbox
# gets, and the host paths it mounts beyond them. They come from
# `.junie/sbx.toml` in the project directory, as `uv` and `pnpm` booleans and a
# `mounts` list of host paths; the booleans default to on and the list to empty.
# `JUNIE_SBX_UV` and `JUNIE_SBX_PNPM` override the booleans for a single run.
# Node and the Playwright browsers follow pnpm: the tree pnpm runs on and the
# browsers it drives go with it.
CONFIG_FILE="${PWD}/.junie/sbx.toml"
UV_ON=1
PNPM_ON=1
CUSTOM_MOUNTS=()

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
# not parse stops the run rather than leaving a setting looking set when it never
# loaded. A mount is a host path, read-only with `:ro`, and `~/` is expanded
# here since TOML has no shell to do it.
read_config_file() {
	python3 - "${CONFIG_FILE}" <<-'PY'
		import os, sys, tomllib

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

		mounts = document.get("mounts", [])
		if not isinstance(mounts, list) or not all(isinstance(entry, str)
		                                           for entry in mounts):
		    sys.stderr.write("junie-sbx: %s: mounts wants a list of host paths\n"
		                     % sys.argv[1])
		    raise SystemExit(1)
		for entry in mounts:
		    # An entry naming no path is refused rather than reduced to the
		    # working directory, which is what `abspath` would make of it.
		    if "\n" in entry or not entry.strip():
		        sys.stderr.write("junie-sbx: %s: mounts names %r, which is no host path\n"
		                         % (sys.argv[1], entry))
		        raise SystemExit(1)
		    # A bare `:ro` names the marker and no path, which is a different
		    # mistake from naming one the host does not have.
		    if entry.endswith(":ro") and not entry[:-3].strip():
		        sys.stderr.write("junie-sbx: %s: mounts names %r, which is only the "
		                         "read-only marker: give the host path before it\n"
		                         % (sys.argv[1], entry))
		        raise SystemExit(1)
		    path = os.path.abspath(os.path.expanduser(entry))
		    ro = ""
		    if entry.endswith(":ro"):
		        # A trailing `:ro` is the marker, so the path it is read off is the
		        # one that gets mounted -- `sbx` strips that suffix itself, and
		        # there is no spelling for a directory whose own name ends in it.
		        # When only that full name exists, the entry cannot mean what it
		        # says, and saying so beats mounting a path the user did not name.
		        stripped_path = os.path.abspath(os.path.expanduser(entry[:-3]))
		        if os.path.exists(path) and not os.path.exists(stripped_path):
		            sys.stderr.write(
		                "junie-sbx: %s: mounts names %r, which is the read-only marker "
		                "on %r -- a trailing :ro always means read-only, so a directory "
		                "whose name ends in one cannot be mounted; rename it\n"
		                % (sys.argv[1], entry, stripped_path))
		            raise SystemExit(1)
		        path, ro = stripped_path, ":ro"
		    if "\n" in entry or not os.path.exists(path):
		        sys.stderr.write("junie-sbx: %s: mounts names %r, which is no host path\n"
		                         % (sys.argv[1], entry))
		        raise SystemExit(1)
		    print("mount=%s%s" % (path, ro))
	PY
}

if [[ -f "${CONFIG_FILE}" ]]; then
	settings="$(read_config_file)"
	while IFS='=' read -r name value; do
		case "${name}" in
			uv) UV_ON="$(switch_value "${value}" "${CONFIG_FILE}")" ;;
			pnpm) PNPM_ON="$(switch_value "${value}" "${CONFIG_FILE}")" ;;
			mount) CUSTOM_MOUNTS+=("${value}") ;;
		esac
	done <<< "${settings}"
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
	mkdir -p "${JUNIE_HOME}/sessions"

	# `-sfn` per name, so a rerun replaces the links rather than failing on
	# the ones an earlier run made.
	local name
	for name in AGENTS.md docs tools; do
		/usr/bin/ln -sfn "${DOT_JUNIE_RO}/${name}" "${JUNIE_HOME}/${name}"
	done

	# Seeded only into a home that has no index yet: the CLI writes that file as
	# it runs, so copying over it on a later run would drop the sessions the
	# sandbox had recorded.
	if [[ ! -e "${JUNIE_HOME}/sessions/index.jsonl" &&
		-f "${DOT_JUNIE_RO}/sessions/index.jsonl" ]]; then
		cp "${DOT_JUNIE_RO}/sessions/index.jsonl" "${JUNIE_HOME}/sessions/"
	fi

	# The host's session directories, linked on every run so one recorded after
	# setup is reachable too. A name the home holds as a real directory is the
	# sandbox's own session, and is left alone; `-n` keeps the link in
	# `sessions/` when the name is already a symlink to a directory.
	local session
	for session in "${DOT_JUNIE_RO}"/sessions/session-*; do
		[[ -e "${session}" ]] || continue
		if [[ -e "${JUNIE_HOME}/sessions/${session##*/}" &&
			! -L "${JUNIE_HOME}/sessions/${session##*/}" ]]; then
			continue
		fi
		/usr/bin/ln -sfn "${session}" "${JUNIE_HOME}/sessions/"
	done
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

# The Junie home links AGENTS.md, docs and tools out of the read-only view
# `setup-junie-sbx.sh` mounts, and copies its session index. An empty view mounts
# without complaint and leaves the sandbox with none of them, so an unfinished
# setup is reported here rather than mounted.
if [[ -z "$(ls -A "${DOT_JUNIE_RO}" 2> /dev/null)" ]]; then
	echo "junie-sbx: ${DOT_JUNIE_RO} is empty or missing; run setup-junie-sbx.sh" >&2
	exit 1
fi

# The home is per-project state this script owns, so it is set up on every run
# rather than once behind a prompt: the links and the session views are rebuilt
# idempotently, which also picks up a host session recorded after setup. Nothing
# here aborts, so a home the host's `~/.junie` cannot fill (it has no `sessions/`
# until the CLI has run there) still leaves a usable home behind.
init_junie_home

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

# The project's own mounts from `.junie/sbx.toml` follow the built-in ones.
if [[ ${#CUSTOM_MOUNTS[@]} -gt 0 ]]; then
	MOUNTS+=("${CUSTOM_MOUNTS[@]}")
fi

# Only the variables a mounted tree needs reach the sandbox: with a switch off
# its paths are absent, which is what leaves `junie-init.sh` linking nothing from
# it. The resolved switches themselves always go, so the startup script knows
# which trees the sandbox was given.
ENVS=(
	--env "JUNIE_HOME=${JUNIE_HOME}"
	--env "USER_HOME=${HOME}"
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

# The provider and model the CLI runs with. `JUNIE_SBX_PROVIDER` and
# `JUNIE_SBX_MODEL` override them. Naming a provider other than the default
# drops the model pin with it, since the deepseek model belongs to that default:
# `JUNIE_SBX_PROVIDER=` leaves the provider to the CLI, which is what makes the
# kit's credentials -- a `JUNIE_API_KEY` or a key the proxy injects -- the ones
# used.
PROVIDER="${JUNIE_SBX_PROVIDER-ollama}"
if [[ -n "${JUNIE_SBX_MODEL+set}" ]]; then
	MODEL="${JUNIE_SBX_MODEL}"
elif [[ "${PROVIDER}" == "ollama" ]]; then
	MODEL="deepseek-v4.1-flash:cloud"
else
	MODEL=""
fi
RUN_ARGS=(run)
if [[ -n "${PROVIDER}" ]]; then
	RUN_ARGS+=(--provider "${PROVIDER}")
fi
if [[ -n "${MODEL}" ]]; then
	RUN_ARGS+=(--model "${MODEL}")
fi

exec sbx "${RUN_ARGS[@]}" \
    "${ENVS[@]}" \
    "${SBX_KIT}" \
    "${MOUNTS[@]}"
