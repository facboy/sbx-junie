# syntax=docker/dockerfile:1.7
# Base image for the `junie` sandbox kit.
#
# The sandbox boots this instead of a bare template because a template carries
# almost none of the libraries a Playwright browser links: without them `chrome`
# exits with "error while loading shared libraries: libglib-2.0.so.0". The
# browsers themselves stay where they are -- the host's Playwright cache, mounted
# in -- so nothing here installs a browser, only what a browser needs to run.
ARG BASE_IMAGE=docker/sandbox-templates:shell-docker
FROM ${BASE_IMAGE}

# Re-declared inside the stage: an ARG defined before the first FROM is a global
# build arg, visible only to FROM lines. Without this, the LABEL below would
# expand to an empty string.
ARG BASE_IMAGE

# The Playwright release whose dependency set this image installs. The set
# tracks that release, so this belongs beside the spec's pinned Playwright: when
# WebKit's bundle starts linking a library that is not here, the browser fails on
# a missing soname and this is what has to move. It also decides which distro the
# host's bundle is built for -- the host cache has to be repopulated with the
# release this pins.
ARG PLAYWRIGHT_VERSION=1.63.0

USER root

# Playwright is installed only to run its own dependency resolver, which knows the
# package names for each distro and so is not a list to keep by hand here. It
# covers the libraries the browsers link, the DRI drivers headless WebKit loads by
# path for EGL, and the fonts fontconfig enumerates. `install-deps` fetches no
# browser, which is the point: the browsers come from the mount, and `--with-deps`
# would download them too. It shells out to `apt-get install` with no frontend of
# its own and there is no tty here, so the export has to cover it. Playwright and
# the indexes are removed in the same layer, leaving what it pulled and nothing
# else.
RUN export DEBIAN_FRONTEND=noninteractive \
 && apt-get update \
 && npm install -g "playwright@${PLAYWRIGHT_VERSION}" \
 && npx playwright install-deps chromium firefox webkit \
 && npm uninstall -g playwright \
 && npm cache clean --force \
 && rm -rf /var/lib/apt/lists/*

# Overrides the base's inherited flavor -- left alone it would report
# "shell-docker" rather than this kit.
LABEL com.docker.sandboxes.flavor="junie"

# Nothing in sbx reads this; records what the floating base resolved to at build
# time, which is the one place the produced image says what it was built on.
LABEL com.docker.sandboxes.base="${BASE_IMAGE}"

# The entrypoint stays the base's `tini`: this image is the environment, and the
# spec's `entrypoint` names the process to run inside it.
