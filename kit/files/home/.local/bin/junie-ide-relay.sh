#!/bin/sh
# Publish the host's JetBrains IDEs to Junie inside the sandbox: one loopback
# endpoint for the project this sandbox holds, plus the instance file Junie
# discovers. `--watch` repeats the pass, so an IDE opened on the host after the
# sandbox started is picked up without restarting the sandbox; a bare run does
# it once. See "Relay architecture" in README.md, and the host steps in
# ../README.md.
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

# Without the daemon there is nothing to dial, so nothing is published and the
# sandbox is left as it was found.
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

# Whether pid $1 is still the endpoint this script started. A recorded pid can be
# handed to an unrelated process once its endpoint dies, so it is confirmed
# against its command line before anything signals it.
is_our_endpoint() {
	[ -r "/proc/$1/cmdline" ] || return 1
	tr '\0' '.' <"/proc/$1/cmdline" 2>/dev/null | grep -q 'junie-ide-relay-endpoint'
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

	if [ -f "$records_dir/$digest" ]; then
		read -r recorded_port recorded_pid <"$records_dir/$digest" || true
		if [ -n "${recorded_pid:-}" ] && kill -0 "$recorded_pid" 2>/dev/null &&
			is_our_endpoint "$recorded_pid"; then
			printf '%s %s\n' "$recorded_port" "$recorded_pid"
			return 0
		fi
		rm -f "$records_dir/$digest"
	fi

	offset=0
	while [ "$offset" -le "$relay_span" ]; do
		candidate=$((relay_port + offset))
		offset=$((offset + 1))

		# Cleared first so that a record left by an endpoint that has since died
		# cannot be mistaken for this attempt's successful bind.
		rm -f "$records_dir/$digest"

		# setsid so the endpoint outlives this script and can be signalled as
		# its own process group.
		setsid python3 "$endpoint" --port "$candidate" --digest "$digest" \
			--host "$ide_host" --host-port "$relay_port" \
			--record "$records_dir/$digest" >/dev/null 2>&1 &

		# The endpoint writes its record only after a successful bind, so this
		# waits for the file rather than for a guessed startup delay.
		waited=0
		while [ "$waited" -lt 30 ] && [ ! -s "$records_dir/$digest" ]; do
			sleep 0.1
			waited=$((waited + 1))
		done
		if [ -s "$records_dir/$digest" ]; then
			read -r bound_port bound_pid <"$records_dir/$digest"
			printf '%s %s\n' "$bound_port" "$bound_pid"
			return 0
		fi
	done
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

	# Stop endpoints for projects the sandbox no longer has open, so that a
	# watcher left running for a long time does not accumulate bound ports.
	for record in "$records_dir"/*; do
		[ -f "$record" ] || continue
		digest=${record##*/}
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

		# Index the instance files to publish by the digest that names them.
		# Keying by digest rather than by path leaves one file per project even
		# when the host has the same project open in two IDEs.
		current = {}
		for source in sorted(glob.glob(os.path.join(host_dir, "*.json"))):
		    try:
		        with open(source) as handle:
		            info = json.load(handle)
		    except Exception:
		        continue
		    digest = hashlib.md5(info["project_path"].encode()).hexdigest()
		    if digest not in served:
		        continue
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
