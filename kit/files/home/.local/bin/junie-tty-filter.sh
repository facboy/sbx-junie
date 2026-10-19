#!/bin/sh
# Run the CLI on a pty and hand it whole input sequences only. See the docstring
# in junie-tty-filter.py for why; the spec's startup hook installs this over
# ~/.local/bin/junie, and removing that hook and this file undoes it.
: "${USER_HOME:?USER_HOME is unset, so the host CLI cannot be found}"
self=$(dirname "$0")
exec python3 "${self}/junie-tty-filter.py" "${USER_HOME}/.local/bin/junie" "$@"
