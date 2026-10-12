#!/usr/bin/env python3
"""Sandbox-side relay endpoint: republishes one host IDE on a loopback port.

`junie-ide-relay.sh` starts one instance per host IDE. Every connection is
opened with a one-line preamble naming that IDE's project digest and forwarded
to the fixed host port the `junie-ide-relayd` daemon listens on; the daemon
resolves the digest to the IDE's current ephemeral port. Nothing here needs to
know that port, which is what lets the kit's egress allowlist name one port.

Written in Python rather than as a `socat SYSTEM:` one-liner: the shell pipeline
that form needs buffers the reply until its input reaches EOF, which stalls any
client that keeps its write side open -- which Junie's own requests do.
"""
import argparse
import os
import select
import socket
import sys
import threading

PREAMBLE_FORMAT = "JUNIE-RELAY 1 %s\n"
IDLE_TIMEOUT = 600


def splice(client, upstream):
    """Relay both directions until each has seen EOF from the other.

    EOF is propagated as a half-close rather than a teardown, so a client that
    shuts down its write side after sending still receives the reply.
    """
    peers = {client: upstream, upstream: client}
    open_directions = {client, upstream}
    try:
        while open_directions:
            readable, _, _ = select.select(list(open_directions), [], [], IDLE_TIMEOUT)
            if not readable:
                return
            for source in readable:
                try:
                    data = source.recv(65536)
                except OSError:
                    open_directions.discard(source)
                    continue
                if not data:
                    open_directions.discard(source)
                    try:
                        peers[source].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    continue
                peers[source].sendall(data)
    finally:
        for sock in peers:
            sock.close()


def handle(conn, digest, host, host_port):
    try:
        upstream = socket.create_connection((host, host_port), timeout=5)
    except OSError as err:
        conn.close()
        sys.stderr.write("junie-ide-relay-endpoint: %s:%d: %s\n" % (host, host_port, err))
        return
    try:
        upstream.sendall((PREAMBLE_FORMAT % digest).encode())
    except OSError:
        conn.close()
        upstream.close()
        return
    upstream.settimeout(None)
    conn.settimeout(None)
    splice(conn, upstream)


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True, help="loopback port to listen on")
    parser.add_argument("--digest", required=True, help="md5 of the project path this endpoint serves")
    parser.add_argument("--host", required=True, help="host the relay daemon runs on")
    parser.add_argument("--host-port", type=int, required=True, help="the relay daemon's port")
    parser.add_argument("--record", required=True,
                        help="file to write '<port> <pid>' to once listening; the "
                             "launcher polls it to tell a bound endpoint from one "
                             "whose port was already taken")
    args = parser.parse_args(argv)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", args.port))
    except OSError as err:
        sys.stderr.write("junie-ide-relay-endpoint: port %d: %s\n" % (args.port, err))
        return 1
    listener.listen(64)
    # Not named `handle`: that is the module-level connection handler above, and
    # a local of the same name would shadow it at the thread target below.
    with open(args.record, "w") as record:
        record.write("%d %d\n" % (args.port, os.getpid()))

    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            continue
        threading.Thread(target=handle,
                         args=(conn, args.digest, args.host, args.host_port),
                         daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
