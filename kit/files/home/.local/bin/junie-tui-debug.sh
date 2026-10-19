#!/bin/sh
# Re-exec the CLI with its TUI debug logging on, which names every CSI sequence
# the input parser reads. The spec's startup hook installs this over
# ~/.local/bin/junie; removing that hook and this file removes the diagnostic.
# The property raises the root logger to DEBUG, so expect a large log.
: "${USER_HOME:?USER_HOME is unset, so the host CLI cannot be found}"
exec env JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:-} -Djunie.logging.debug.enabled=true" \
	"${USER_HOME}/.local/bin/junie" "$@"
