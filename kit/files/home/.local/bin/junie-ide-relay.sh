#!/bin/sh
# Publish the host's JetBrains IDEs to Junie inside the sandbox.
#
# Junie CLI finds an IDE by reading `<Junie Home>/instances/*.json` and then
# connecting to `127.0.0.1:<port>`, which inside the sandbox is the sandbox's
# own loopback, where no IDE runs. This script reads the host's instance files
# from the read-only mount the kit documents (see README.md), starts one
# loopback endpoint per host IDE, and writes an instance file naming that
# endpoint's port and its pid, which is what discovery requires to be a live
# process here.
#
# Only the project this sandbox holds is published. A host IDE can have several
# projects open and writes one instance file per project; Junie picks an IDE by
# project path, so a file for any other project would offer it an IDE that has a
# different project open.
#
# Every endpoint forwards to one fixed host port -- the `junie-ide-relayd`
# daemon the kit documents -- naming the project its connection belongs to in a
# one-line preamble. The daemon owns the mapping from project to the IDE's
# ephemeral port, which is what keeps the kit's egress allowlist down to that
# one port. The IDE's own port is never dialled from here and never named in the
# allowlist.
#
# Invoked through `sh` because files under `files/home/` land 0644 (see
# picoclaw). Idempotent: a no-op without the mount, without the daemon, or
# without a running IDE; it reuses an endpoint an earlier start left running,
# and it only ever deletes instance files that name its own projects. Endpoint
# pids are recorded under Junie Home rather than matched by command line, so
# nothing here depends on `pgrep`.
set -u

# Startup commands run with a minimal environment: no $HOME, and nothing
# guarantees PATH names the tools below.
export PATH="${PATH:-/usr/local/bin:/usr/bin:/bin}"
JUNIE_HOME_DIR="${JUNIE_HOME:-/home/agent/.junie}"
instances_dir="$JUNIE_HOME_DIR/instances"
records_dir="$JUNIE_HOME_DIR/ide-relay"
host_dir="${JUNIE_IDE_HOST_DIR:-}"
ide_host="${JUNIE_IDE_HOST:-host.docker.internal}"
relay_port="${JUNIE_IDE_RELAY_PORT:-27183}"
relay_span="${JUNIE_IDE_RELAY_PORT_SPAN:-20}"
endpoint="${JUNIE_IDE_ENDPOINT:-/home/agent/.local/bin/junie-ide-relay-endpoint.py}"

command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$endpoint" ] || exit 0

# Locate the host's instance directory. `sbx create` mounts an extra read-only
# workspace at the path the host named, so this arrives as the host's own
# `$HOME/.junie/instances`. JUNIE_IDE_HOST_DIR wins when set; otherwise take the
# first read-only workspace holding an instance file, which needs no guess at
# the host's home layout.
if [ -z "$host_dir" ]; then
	while read -r _ mount_point mount_type mount_options _; do
		[ "$mount_type" = virtiofs ] || continue
		case ",$mount_options," in *,ro,*) ;; *) continue ;; esac
		[ -d "$mount_point" ] || continue
		if ls "$mount_point"/????????????????????????????????_*.json >/dev/null 2>&1; then
			host_dir=$mount_point
			break
		fi
	done </proc/mounts
fi
[ -n "$host_dir" ] && [ -d "$host_dir" ] || exit 0

# The sandbox's own project is the writable workspace mount, which `sbx create`
# places at the host's path. JUNIE_IDE_PROJECT wins when set, for a sandbox that
# mounts the project somewhere other than the path it was created with.
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

mkdir -p "$instances_dir" "$records_dir"
relays=$(mktemp)
wanted=$(mktemp)
trap 'rm -f "$relays" "$wanted"' EXIT

# Without the daemon there is nothing to dial, so nothing is published and the
# sandbox is left as it was found. Tested once, before any endpoint is started,
# so an unstarted daemon costs one connection rather than one per IDE.
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

# Echo "<relay_port> <relay_pid>" for an endpoint serving project digest $1,
# reusing one an earlier start left running. An endpoint's identity is its
# digest, not its port: every endpoint dials the same fixed host port, so two
# endpoints for different projects are told apart only by the preamble they
# send. The record is therefore keyed by digest. Starting one is a matter of
# binding the next free port: the endpoint writes its own record once it is
# listening, so a port another process already holds is detected by that
# endpoint failing to bind, not by guessing at what might be running there.
relay_for_digest() {
	digest=$1

	if [ -f "$records_dir/$digest" ]; then
		read -r recorded_port recorded_pid <"$records_dir/$digest" || true
		if [ -n "${recorded_pid:-}" ] && kill -0 "$recorded_pid" 2>/dev/null; then
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

daemon_healthy || exit 0

# The instance files that name this sandbox's own project, as "<digest> <path>"
# lines. Tab-separated so that a project path containing spaces survives.
python3 - "$host_dir" "$sandbox_project" <<'PY' >"$wanted"
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

python3 - "$host_dir" "$instances_dir" "$relays" <<'PY'
import glob, hashlib, json, os, sys

host_dir, instances_dir, relays_path = sys.argv[1], sys.argv[2], sys.argv[3]

# "<digest> <project_path> <relay_port> <relay_pid>", one line per project served.
served = {}
with open(relays_path) as handle:
    for line in handle:
        digest, project, relay_port, relay_pid = line.split("\t", 3)
        served[digest] = (int(relay_port), int(relay_pid))

# Index the instance files to publish by the digest that names them. Keying by
# digest rather than by path leaves one file per project even when the host has
# the same project open in two IDEs. Each endpoint is listed only for the project
# it was opened for, so a connection re-resolved by the daemon goes to that
# project's IDE.
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

# Instance files are named <md5(project_path)>_<pid>.json. A file is stale when
# its digest is not served at all -- it names a project this sandbox does not
# hold, or an IDE that has since closed -- or when it names an older endpoint pid
# than the one serving that project. Either leaves the CLI an endpoint it reads
# as a live IDE for the wrong project. Remove both kinds, identified by the
# project's digest; a file that does not follow the naming convention is none of
# this script's business.
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
