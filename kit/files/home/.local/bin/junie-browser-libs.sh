#!/bin/sh

# Register the host's copies of Chromium's runtime libraries with the image's
# loader.
#
# The browsers come from the host's Playwright cache, but the base image carries
# none of the libraries they link, so `chrome` exits with "error while loading
# shared libraries: libglib-2.0.so.0". A host that can run Chromium has those
# libraries already -- Playwright checks for them on the host and prints the apt
# command when they are absent -- so they are read from there rather than
# installed here.
#
# Only names the image cannot resolve are registered. A host libc.so.6 or
# libm.so.6 on the loader's path is fatal: every binary in the image dies before
# it starts with "undefined symbol __nptl_change_stack_perm, version
# GLIBC_PRIVATE". The host script keeps those out of the mount and the check
# below keeps them out again, so this path cannot shadow the image's own.

set -eu

CONFIG_HOME="${USER_CONFIG_HOME:-${HOME}/.config/junie-sbx}"
HOST_LIBS="${JUNIE_HOST_BROWSER_LIBS:-${CONFIG_HOME}/browser-libs}"
MERGED=/opt/browser-libs
CONF=/etc/ld.so.conf.d/zz-host-browser-libs.conf

# Idempotent across restarts: the entry is dropped and the cache rebuilt from
# the image alone first, so a name registered by an earlier start cannot read as
# something the image itself resolves. Without this the second start of a
# sandbox would filter out everything it registered the first time.
rm -f "${CONF}"
rm -rf "${MERGED}"
ldconfig

if [ ! -d "${HOST_LIBS}" ]; then
	echo "junie-browser-libs: ${HOST_LIBS} is not mounted, so the host's browser libraries are unavailable" >&2
	echo "  Chromium will not start unless this image already carries its runtime libraries." >&2
	exit 0
fi

mkdir -p "${MERGED}"
have="$(ldconfig -p | sed -n 's/.*=> \///; s/.*\///p')"

for lib in "${HOST_LIBS}"/*; do
	[ -e "${lib}" ] || continue
	name="${lib##*/}"
	if printf '%s\n' "${have}" | grep -qxF "${name}"; then
		continue
	fi
	ln -s "${lib}" "${MERGED}/${name}"
done

count="$(find "${MERGED}" -type l | wc -l)"
printf '%s\n' "${MERGED}" > "${CONF}"
ldconfig

if ldconfig -p | grep -q 'libglib-2.0\.so\.0'; then
	echo "junie-browser-libs: registered ${count} libraries from the host's browser dependency set"
else
	echo "junie-browser-libs: libglib-2.0.so.0 does not resolve, so Chromium will not start." >&2
	echo "  ${HOST_LIBS} holds ${count} usable libraries; the host's set is incomplete or was" >&2
	echo "  built for a newer C library than this image provides." >&2
fi
