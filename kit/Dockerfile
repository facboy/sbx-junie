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
# a missing soname and this is what has to move.
ARG PLAYWRIGHT_VERSION=1.63.0

USER root

# Playwright is installed only to run its own dependency resolver, which knows the
# package names for each distro and so is not a list to keep by hand here.
# `install-deps` fetches no browser, which is the point: the browsers come from
# the mount, and `--with-deps` would download them too. It is removed again in the
# same layer, so the image carries the packages it pulled and nothing of its own.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates npm \
 && npm install -g "playwright@${PLAYWRIGHT_VERSION}" \
 && npx playwright install-deps chromium firefox webkit \
 && npm uninstall -g playwright \
 && npm cache clean --force

# Three things the dependency set does not cover, all read from disk at run time
# rather than linked:
#
# - DRI drivers. Headless WebKit renders through EGL, which is loaded by path
#   from a driver directory rather than by soname; with no software rasteriser it
#   aborts with "Could not create EGL display: no supported platform available".
# - Fonts. fontconfig enumerates these; without any, text measures as zero-width.
# - noble's ICU 74 and libxml2.so.2. WebKit publishes one bundle per distro, and
#   a host cache populated under 24.04 holds one that links these by soname. This
#   base has ICU 78 and libxml2.so.16 instead, and ICU embeds its major version in
#   every symbol name, so a newer one cannot satisfy an older link. They are
#   extracted rather than installed, because installing noble's `libxml2` would
#   replace this image's own, which everything else here links. A cache populated
#   under this base's own distro needs none of this and is unaffected by it.
RUN printf 'deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] http://archive.ubuntu.com/ubuntu noble main\n' \
      > /etc/apt/sources.list.d/zz-noble-abi.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libgl1-mesa-dri libegl-mesa0 libgbm1 fonts-liberation \
 && mkdir -p /tmp/debs && cd /tmp/debs \
 && apt-get download libicu74 libxml2 \
 && for d in *.deb; do dpkg-deb -x "$d" /tmp/noble; done \
 && cp -a /tmp/noble/usr/lib/x86_64-linux-gnu/libicu*.so.74* /usr/lib/x86_64-linux-gnu/ \
 && cp -a /tmp/noble/usr/lib/x86_64-linux-gnu/libxml2.so.2* /usr/lib/x86_64-linux-gnu/ \
 && rm -rf /tmp/debs /tmp/noble /etc/apt/sources.list.d/zz-noble-abi.list \
 && rm -rf /var/lib/apt/lists/* \
 && ldconfig

# Overrides the base's inherited flavor -- left alone it would report
# "shell-docker" rather than this kit.
LABEL com.docker.sandboxes.flavor="junie"

# Nothing in sbx reads this; records what the floating base resolved to at build
# time, which is the one place the produced image says what it was built on.
LABEL com.docker.sandboxes.base="${BASE_IMAGE}"

# The entrypoint stays the base's `tini`: this image is the environment, and the
# spec's `entrypoint` names the process to run inside it.
