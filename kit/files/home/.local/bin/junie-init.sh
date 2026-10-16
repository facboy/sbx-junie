#!/bin/sh

mkdir -p ~/.local/share/
/usr/bin/ln -s "${USER_HOME}/.local/share/junie" ~/.local/share/

mkdir -p ~/.local/bin/
/usr/bin/ln -sf "${USER_HOME}/.local/bin/junie" ~/.local/bin/

# Junie's instances do not follow JUNIE_HOME -- the CLI resolves them from
# user.home/.junie/instances -- so ~/.junie is a real directory the relay fills,
# and the read-only view is linked beside them rather than that directory moved.
if [ -n "${JUNIE_HOME:-}" ]; then
	mkdir -p ~/.junie
	for name in AGENTS.md docs tools; do
		/usr/bin/ln -sfn "${JUNIE_HOME}/${name}" ~/.junie/
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
	/usr/bin/ln -sf "$1" ~/.local/bin/
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
NPMRC=~/.npmrc
if [ -n "${JUNIE_PNPM_STORE:-}" ]; then
	if ! grep -q '^store-dir=' "${NPMRC}" 2> /dev/null; then
		echo "store-dir=${JUNIE_PNPM_STORE}" >> "${NPMRC}"
	fi
fi
if [ -n "${JUNIE_PNPM_CACHE:-}" ]; then
	if ! grep -q '^cache-dir=' "${NPMRC}" 2> /dev/null; then
		echo "cache-dir=${JUNIE_PNPM_CACHE}" >> "${NPMRC}"
	fi
fi
