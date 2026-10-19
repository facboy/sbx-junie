#!/bin/sh
# Run the CLI on a pty and refuse all-motion mouse reporting. See the docstring
# in junie-mouse-filter.py for why; the spec's startup hook installs this over
# ~/.local/bin/junie, and removing that hook and this file undoes it.
: "${USER_HOME:?USER_HOME is unset, so the host CLI cannot be found}"
self=$(dirname "$0")
exec python3 "${self}/junie-mouse-filter.py" "${USER_HOME}/.local/bin/junie" "$@"
