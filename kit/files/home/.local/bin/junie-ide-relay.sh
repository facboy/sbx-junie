#!/bin/sh
# Publish the host's JetBrains IDEs to Junie inside the sandbox: one loopback
# endpoint for the project this sandbox holds, plus the instance file Junie
# discovers, and a mirror of the IDE's built-in MCP server. `--watch` repeats
# the pass, so an IDE opened on the host after the sandbox started is picked up
# without restarting the sandbox; a bare run does it once. See "Relay
# architecture" in README.md, and the host steps in ../README.md.
#
# Invoked through `sh` because files under `files/home/` land 0644 (see
# picoclaw). Endpoint pids are recorded under Junie Home and reuse is keyed by
# project, so nothing here matches on a command line unless it is about to
# signal a process.
set -u

# Startup commands run with a minimal environment: no $HOME, and nothing
# guarantees PATH names the tools below.
export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"

# Junie's own state follows Junie Home, so the relay's bookkeeping does too.
junie_home="${JUNIE_HOME:-/home/agent/.junie}"
records_dir="$junie_home/ide-relay"
watcher_pidfile="$junie_home/ide-relay.pid"

# The instance directory does NOT follow Junie Home: the CLI resolves it from
# the `junie.instances.dir` system property, else `user.home/.junie/instances`,
# and never consults JUNIE_HOME. Publishing under Junie Home therefore leaves
# `/ide` reporting no IDE whenever JUNIE_HOME is set, so this names the path the
# CLI reads. JUNIE_IDE_INSTANCES_DIR tracks a `-Djunie.instances.dir` override.
instances_dir="${JUNIE_IDE_INSTANCES_DIR:-/home/agent/.junie/instances}"
host_dir="${JUNIE_IDE_HOST_DIR:-}"
ide_host="${JUNIE_IDE_HOST:-host.docker.internal}"
relay_port="${JUNIE_IDE_RELAY_PORT:-27183}"
relay_span="${JUNIE_IDE_RELAY_PORT_SPAN:-20}"
endpoint="${JUNIE_IDE_ENDPOINT:-/home/agent/.local/bin/junie-ide-relay-endpoint.py}"
interval="${JUNIE_IDE_WATCH_INTERVAL:-10}"

watch=0
case "${1:-}" in
--watch) watch=1 ;;
esac

command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$endpoint" ] || exit 0

# The sandbox's own project is its writable workspace mount, which `sbx create`
# places at the host's path. A host IDE can have several projects open, one
# instance file each, and Junie picks an IDE by project path -- so an instance
# file for any other project would offer it an IDE with a different project
# open. JUNIE_IDE_PROJECT wins when set, for a project mounted elsewhere.
sandbox_project="${JUNIE_IDE_PROJECT:-}"
if [ -z "$sandbox_project" ]; then
	while read -r _ mount_point mount_type mount_options _; do
		[ "$mount_type" = virtiofs ] || continue
		case ",$mount_options," in *,ro,*) continue ;; esac
		case "$mount_point" in /etc/*) continue ;; esac
		[ -d "$mount_point" ] || continue
		sandbox_project=$mount_point
		break
	done </proc/mounts
fi
[ -n "$sandbox_project" ] || sandbox_project=$PWD

# Locate the host's instance directory. `sbx create` mounts an extra read-only
# workspace at the path the host named, so this arrives as the host's own
# `$HOME/.junie/instances`. JUNIE_IDE_HOST_DIR wins when set. That directory is
# empty until the IDE plugin first runs, so a mount without instance files is
# still the one wanted; the sandbox's own project is excluded by path.
find_host_dir() {
	[ -n "${JUNIE_IDE_HOST_DIR:-}" ] && { printf '%s\n' "$JUNIE_IDE_HOST_DIR"; return 0; }
	while read -r _ mount_point mount_type mount_options _; do
		[ "$mount_type" = virtiofs ] || continue
		case ",$mount_options," in *,ro,*) ;; *) continue ;; esac
		case "$mount_point" in /etc/*) continue ;; esac
		[ "$mount_point" = "$sandbox_project" ] && continue
		[ -d "$mount_point" ] || continue
		printf '%s\n' "$mount_point"
		return 0
	done </proc/mounts
	return 1
}

host_dir=$(find_host_dir) || exit 0
[ -d "$host_dir" ] || exit 0

mkdir -p "$instances_dir" "$records_dir"
relays=$(mktemp)
wanted=$(mktemp)
trap 'rm -f "$relays" "$wanted"' EXIT

# The protocol version the daemon must speak for the MCP preambles to be
# answered. A daemon started before they were added closes those connections
# silently, which reads as a dead port, so its absence is reported rather than
# left to look like an IDE that is down.
required_daemon_version=2

# Without the daemon there is nothing to dial, so nothing is published and the
# sandbox is left as it was found. The version is checked separately: the RPC
# relay works against any daemon, so an older one costs only the MCP mirror.
daemon_healthy() {
	reply=$(python3 -c '
import socket, sys
try:
    conn = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)
    conn.sendall(b"JUNIE-RELAY 1 health\n")
    sys.stdout.write(conn.recv(64).decode("ascii", "replace"))
except Exception:
    pass' "$ide_host" "$relay_port" 2>/dev/null)
	case $reply in
	'JUNIE-RELAY 1 ok'*) return 0 ;;
	esac
	return 1
}

# The protocol version the daemon reports, or nothing when it is older than the
# version banner itself. Only a daemon that names one has been restarted since
# the banner was added, so this is what tells the two apart.
daemon_version() {
	reply=$(python3 -c '
import socket, sys
try:
    conn = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)
    conn.sendall(b"JUNIE-RELAY 1 health\n")
    sys.stdout.write(conn.recv(64).decode("ascii", "replace"))
except Exception:
    pass' "$ide_host" "$relay_port" 2>/dev/null)
	case $reply in
	'JUNIE-RELAY 1 ok '*) printf '%s' "${reply#JUNIE-RELAY 1 ok }" ;;
	esac
}

# Set once per run, so a daemon that needs restarting is named in the log rather
# than reported on every tick.
stale_daemon_reported=0

# Whether pid $1 is still the endpoint this script started. A recorded pid can be
# handed to an unrelated process once its endpoint dies, so it is confirmed
# against its command line before anything signals it.
is_our_endpoint() {
	[ -r "/proc/$1/cmdline" ] || return 1
	tr '\0' '.' <"/proc/$1/cmdline" 2>/dev/null | grep -q 'junie-ide-relay-endpoint'
}

# The IDE's built-in MCP port, as "<outcome> [port]". The daemon resolves it by
# asking the plugin over the RPC relay, and answers with one of:
#
#   port <n>  the plugin named a port to mirror
#   none      the plugin answered without a built-in server
#   gone      no instance file names the digest, so that IDE is not running
#   unknown   the plugin did not answer, or answered unusably
#
# `none` and `gone` free a mirror the caller holds -- the port is the IDE's, and
# nothing will use it again. `unknown` must not: a momentary RPC failure answers
# the same way, and tearing down a working mirror for it would drop the IDE MCP
# connection until the next pass rebuilt it.
mcp_port_for_digest() {
	reply=$(python3 -c '
import socket, sys
try:
    conn = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=5)
    conn.sendall(("JUNIE-RELAY 1 mcp-port %s\n" % sys.argv[3]).encode())
    sys.stdout.write(conn.recv(64).decode("ascii", "replace"))
except Exception:
    pass' "$ide_host" "$relay_port" "$1" 2>/dev/null)
	case $reply in
	'JUNIE-RELAY 1 mcp-port '*) reply=${reply#JUNIE-RELAY 1 mcp-port } ;;
	*) printf 'unknown\n' ; return 0 ;;
	esac
	case $reply in
	none|gone) printf '%s\n' "$reply" ;;
	*[!0-9]*|'') printf 'unknown\n' ;;
	*) printf 'port %s\n' "$reply" ;;
	esac
}

# Stop the mirror for digest $1 and drop its record, freeing the port the IDE no
# longer serves. A record whose pid is gone or is not an endpoint of ours is
# removed without a signal.
stop_mcp_mirror() {
	record="$records_dir/mcp-$1"
	[ -f "$record" ] || return 0
	read -r _ endpoint_pid <"$record" || true
	if [ -n "${endpoint_pid:-}" ] && is_our_endpoint "$endpoint_pid"; then
		kill "$endpoint_pid" 2>/dev/null
	fi
	rm -f "$record"
}

# Echo "<relay_port> <relay_pid>" for an endpoint serving project digest $1,
# reusing one an earlier pass left running. An endpoint's identity is its digest,
# not its port: every endpoint dials the same fixed host port, so two endpoints
# for different projects are told apart only by the preamble they send, and the
# record is keyed by digest. Starting one binds the next free port: the endpoint
# writes its record only once it is listening, so a port another process holds is
# detected by that bind failing rather than by guessing at what runs there.
relay_for_digest() {
	digest=$1
	record="$records_dir/$digest"

	if [ -f "$record" ]; then
		read -r recorded_port recorded_pid <"$record" || true
		if [ -n "${recorded_pid:-}" ] && kill -0 "$recorded_pid" 2>/dev/null &&
			is_our_endpoint "$recorded_pid"; then
			printf '%s %s\n' "$recorded_port" "$recorded_pid"
			return 0
		fi
		rm -f "$record"
	fi

	offset=0
	while [ "$offset" -le "$relay_span" ]; do
		candidate=$((relay_port + offset))
		offset=$((offset + 1))

		# Cleared first so that a record left by an endpoint that has since died
		# cannot be mistaken for this attempt's successful bind.
		rm -f "$record"

		# setsid so the endpoint outlives this script and can be signalled as
		# its own process group.
		setsid python3 "$endpoint" --port "$candidate" --digest "$digest" \
			--host "$ide_host" --host-port "$relay_port" \
			--record "$record" >/dev/null 2>&1 &

		# The endpoint writes its record only after a successful bind, so this
		# waits for the file rather than for a guessed startup delay.
		waited=0
		while [ "$waited" -lt 30 ] && [ ! -s "$record" ]; do
			sleep 0.1
			waited=$((waited + 1))
		done
		if [ -s "$record" ]; then
			read -r bound_port bound_pid <"$record"
			printf '%s %s\n' "$bound_port" "$bound_pid"
			return 0
		fi
	done
	return 1
}

# Echo "<port> <pid>" for an endpoint mirroring project digest $1's built-in MCP
# server, which the IDE serves on port $2. The port is the IDE's own rather than
# one of ours: the CLI resolves it over the RPC relay and then dials
# localhost:<port> itself, so the mirror has to hold that exact port. The IDE
# picks a new one when it restarts, so a record naming any other port is dropped
# -- and its endpoint killed, which unbinds the port the old IDE left behind --
# before the new bind. One already bound to the port the IDE serves now is kept.
relay_mcp_for_digest() {
	digest=$1
	mcp_port=$2
	record="$records_dir/mcp-$digest"

	if [ -f "$record" ]; then
		read -r recorded_port recorded_pid <"$record" || true
		if [ -n "${recorded_pid:-}" ] && kill -0 "$recorded_pid" 2>/dev/null &&
			is_our_endpoint "$recorded_pid"; then
			if [ "$recorded_port" = "$mcp_port" ]; then
				printf '%s %s\n' "$recorded_port" "$recorded_pid"
				return 0
			fi
			kill "$recorded_pid" 2>/dev/null
		fi
		rm -f "$record"
	fi

	setsid python3 "$endpoint" --port "$mcp_port" --digest "$digest" --mcp \
		--host "$ide_host" --host-port "$relay_port" \
		--record "$record" >/dev/null 2>&1 &

	waited=0
	while [ "$waited" -lt 30 ] && [ ! -s "$record" ]; do
		sleep 0.1
		waited=$((waited + 1))
	done
	if [ -s "$record" ]; then
		read -r bound_port bound_pid <"$record"
		printf '%s %s\n' "$bound_port" "$bound_pid"
		return 0
	fi
	return 1
}

# One pass over the host's instance files. Returns early rather than exiting when
# the daemon is down, so that a watcher retries it on the next tick.
relay_pass() {
	[ -d "$host_dir" ] || return 0
	daemon_healthy || return 0

	: >"$relays"
	: >"$wanted"

	# The instances naming this sandbox's own project, as "<digest>\t<path>"
	# lines: tab-separated so a project path with spaces stays one field.
	python3 - "$host_dir" "$sandbox_project" <<-'PY' >"$wanted"
		import glob, hashlib, json, os, sys

		host_dir, sandbox_project = sys.argv[1], sys.argv[2]
		sandbox_root = os.path.realpath(sandbox_project)

		for source in sorted(glob.glob(os.path.join(host_dir, "*.json"))):
		    try:
		        with open(source) as handle:
		            project = json.load(handle).get("project_path")
		    except Exception:
		        continue
		    if not isinstance(project, str):
		        continue
		    resolved = os.path.realpath(project)
		    if resolved != sandbox_root and not resolved.startswith(sandbox_root + os.sep):
		        continue
		    print(hashlib.md5(project.encode()).hexdigest(), project, sep="\t")
	PY

	while IFS='	' read -r digest project; do
		[ -n "$digest" ] || continue
		grep -q "^$digest	" "$relays" && continue

		if relay=$(relay_for_digest "$digest"); then
			# `relay` is "<port> <pid>"; split it so the record is tab-separated
			# throughout and a project path with spaces stays one field.
			set -- $relay
			printf '%s\t%s\t%s\t%s\n' "$digest" "$project" "$1" "$2" >>"$relays"
		fi
	done <"$wanted"

	# The built-in MCP server, on the port the IDE chose rather than one named
	# here: the CLI dials localhost:<port> for it, so the mirror binds that port.
	# Asked every pass, since the IDE picks a new port when it restarts, and the
	# mirror drops the listener it left on the old one. An outcome that frees the
	# port -- `none` or `gone` -- stops the mirror, so one is not held for the
	# IDE's port after the IDE gave it up.
	#
	# A daemon too old to answer this preamble closes the connection, which is
	# the same shape as `unknown`. Skipped instead, so that the mirror the daemon
	# already holds is left running and the IDE relay above is unaffected.
	version=$(daemon_version)
	if [ -n "$version" ] && [ "$version" -ge "$required_daemon_version" ] 2>/dev/null; then
		while IFS='	' read -r digest project; do
			[ -n "$digest" ] || continue
			resolution=$(mcp_port_for_digest "$digest")
			set -- $resolution
			case "${1:-}" in
			port) relay_mcp_for_digest "$digest" "$2" >/dev/null || true ;;
			none|gone) stop_mcp_mirror "$digest" ;;
			esac
		done <"$wanted"
	elif [ "$stale_daemon_reported" = 0 ]; then
		stale_daemon_reported=1
		echo "junie-ide-relay: the host junie-ide-relayd speaks protocol" \
			"${version:-1}, this relay needs $required_daemon_version:" \
			"restart it for the IDE's MCP server to be relayed" >&2
	fi

	# Stop endpoints for projects the sandbox no longer has open, so that a
	# watcher left running for a long time does not accumulate bound ports. An
	# MCP record is keyed "mcp-<digest>", so both kinds name the same project
	# and both are kept while that project is open.
	for record in "$records_dir"/*; do
		[ -f "$record" ] || continue
		name=${record##*/}
		digest=${name#mcp-}
		grep -q "^$digest	" "$wanted" && continue
		# The record is "<port> <pid>"; the pid is the field to signal.
		read -r _ endpoint_pid <"$record" || true
		if [ -n "${endpoint_pid:-}" ] && is_our_endpoint "$endpoint_pid"; then
			kill "$endpoint_pid" 2>/dev/null
		fi
		rm -f "$record"
	done

	python3 - "$host_dir" "$instances_dir" "$relays" <<-'PY'
		import glob, hashlib, json, os, sys

		host_dir, instances_dir, relays_path = sys.argv[1], sys.argv[2], sys.argv[3]

		# "<digest>\t<project_path>\t<relay_port>\t<relay_pid>", one line each.
		served = {}
		with open(relays_path) as handle:
		    for line in handle:
		        digest, project, relay_port, relay_pid = line.split("\t", 3)
		        served[digest] = (int(relay_port), int(relay_pid))

		# Index the file to publish per digest, taking the one the host daemon
		# resolves that digest to -- the newest file naming the project, tie-broken
		# by port -- since that is the IDE the connection is spliced to. The CLI
		# authenticates with the token published here, so a token from any other
		# file is one that IDE refuses: an IDE with the project open twice is what
		# produced the 401 this avoids. Keying by digest rather than by path still
		# leaves one file per project, even when the host has it open in two IDEs.
		newest = {}
		for source in sorted(glob.glob(os.path.join(host_dir, "*.json"))):
		    try:
		        with open(source) as handle:
		            info = json.load(handle)
		        port = int(info["port"])
		        if not 1 <= port <= 65535:
		            continue
		        digest = hashlib.md5(info["project_path"].encode()).hexdigest()
		    except Exception:
		        continue
		    if digest not in served:
		        continue
		    stamp = (os.stat(source).st_mtime, port)
		    if digest not in newest or stamp > newest[digest][0]:
		        newest[digest] = (stamp, info)

		current = {}
		for digest, (_, info) in newest.items():
		    info["port"], info["pid"] = served[digest]
		    current[digest] = info

		# Instance files are named <md5(project_path)>_<pid>.json. A file is stale
		# when its digest is not served -- the project is not this sandbox's -- or
		# when it names an older endpoint pid than the one serving it. Either
		# leaves the CLI an endpoint it reads as a live IDE for the wrong project.
		for existing in glob.glob(os.path.join(instances_dir, "*.json")):
		    name = os.path.basename(existing)
		    digest, _, suffix = name.partition("_")
		    if len(digest) != 32 or not suffix.endswith(".json"):
		        continue
		    if digest in current and name == "%s_%d.json" % (digest, current[digest]["pid"]):
		        continue
		    os.remove(existing)

		for name, info in (("%s_%d.json" % (d, i["pid"]), i) for d, i in current.items()):
		    with open(os.path.join(instances_dir, name), "w") as handle:
		        json.dump(info, handle)
	PY
}

relay_pass

[ "$watch" = 1 ] || exit 0

# One watcher per sandbox: a second start finds the first alive and leaves it be.
if [ -f "$watcher_pidfile" ]; then
	read -r existing <"$watcher_pidfile" || true
	if [ -n "${existing:-}" ] && kill -0 "$existing" 2>/dev/null; then
		exit 0
	fi
fi
printf '%s\n' "$$" >"$watcher_pidfile"

while :; do
	sleep "$interval"
	relay_pass
done
