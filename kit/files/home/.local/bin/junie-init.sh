#!/bin/sh

# The home every link below lands in. `sh` expands `~` from `$HOME`, and a
# startup command can run without one: the links would then be made in a literal
# `~` directory in the working directory, where the entrypoint never looks. The
# sandbox's user is this one either way, so its home is the fallback.
home="${HOME:-/home/agent}"

mkdir -p "${home}/.local/share/"
/usr/bin/ln -s "${USER_HOME}/.local/share/junie" "${home}/.local/share/"

mkdir -p "${home}/.local/bin/"
/usr/bin/ln -sf "${USER_HOME}/.local/bin/junie" "${home}/.local/bin/"

# Junie's instances do not follow JUNIE_HOME -- the CLI resolves them from
# user.home/.junie/instances -- so ~/.junie is a real directory the relay fills,
# and the read-only view is linked beside them rather than that directory moved.
if [ -n "${JUNIE_HOME:-}" ]; then
	mkdir -p "${home}/.junie"
	for name in AGENTS.md docs tools; do
		/usr/bin/ln -sfn "${JUNIE_HOME}/${name}" "${home}/.junie/"
	done
fi

# The host trees are mounted read-only at their own paths, so each of these is
# the host's own tool. ~/.local/bin leads PATH, which is what makes the host's
# uv, pnpm and node the ones that run. Each link is made only if the binary
# actually executes here: the host's build can be newer than this image's libc,
# and a link that cannot run would only shadow the tool the image ships.
link_bin() {
	[ -x "$1" ] || return 0
	"$1" --version > /dev/null 2>&1 || return 0
	/usr/bin/ln -sf "$1" "${home}/.local/bin/"
}

# node goes first: the node-hosted tools below are scripts whose shebang
# resolves node from PATH, so they cannot be tested until node is linked.
# fnm points its `default` alias at the active node version; fall back to
# whatever is installed when that alias is absent. pnpm keys its store layout to
# the pnpm that reads it, and that pnpm needs a node it can run on.
if [ "${JUNIE_SBX_PNPM:-1}" = 1 ]; then
	for dir in "${USER_HOME}/.local/share/fnm/aliases/default/installation/bin" \
		"${USER_HOME}/.local/share/fnm/node-versions/"*/installation/bin; do
		if [ -x "${dir}/node" ]; then
			for bin in node npm npx corepack pnpx; do
				link_bin "${dir}/${bin}"
			done
			break
		fi
	done
fi

# Each tree is linked only when its switch left it on: the switches come from
# the project's `.junie/sbx.toml`, and a tree left off is not mounted, so its
# host path is absent here and the test below would skip it anyway.
if [ "${JUNIE_SBX_UV:-1}" = 1 ]; then
	link_bin "${USER_HOME}/.local/bin/uv"
	link_bin "${USER_HOME}/.local/bin/uvx"
fi
if [ "${JUNIE_SBX_PNPM:-1}" = 1 ]; then
	link_bin "${PNPM_HOME}/bin/pnpm"
fi

# pnpm reads store and cache locations from .npmrc. Writing them here rather
# than exporting them keeps one source of truth for the two, and .npmrc is
# where pnpm looks before it looks anywhere else.
#
# Each key is rewritten on every start, not added only when absent: a line an
# earlier sandbox wrote names an overlay this one may not have mounted, and
# pnpm would then keep its store and cache where nothing is.
NPMRC="${home}/.npmrc"
set_npmrc() {
	[ -f "${NPMRC}" ] || : > "${NPMRC}"
	NPMRC_KEY="$1" NPMRC_VALUE="$2" awk '
		index($0, ENVIRON["NPMRC_KEY"] "=") == 1 { next }
		{ print }
		END { print ENVIRON["NPMRC_KEY"] "=" ENVIRON["NPMRC_VALUE"] }
	' "${NPMRC}" > "${NPMRC}.new" && cat "${NPMRC}.new" > "${NPMRC}"
	rm -f "${NPMRC}.new"
}
if [ -n "${JUNIE_PNPM_STORE:-}" ]; then
	set_npmrc store-dir "${JUNIE_PNPM_STORE}"
fi
if [ -n "${JUNIE_PNPM_CACHE:-}" ]; then
	set_npmrc cache-dir "${JUNIE_PNPM_CACHE}"
fi
