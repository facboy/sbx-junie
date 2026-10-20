#!/usr/bin/env python3
"""Sandbox-side relay endpoint: republishes one host IDE on a loopback port.

`junie-ide-relay.sh` starts one instance per host IDE. Every connection is
opened with a one-line preamble naming that IDE's project digest and the secret
this sandbox was provisioned with, and forwarded to the fixed host port the
`junie-ide-relayd` daemon listens on; the daemon checks the secret against the
IDE's project and resolves the digest to that IDE's current ephemeral port.
Nothing here needs to know that port, which is what lets the kit's egress
allowlist name one port.

With `--mcp` the preamble instead asks for the IDE's built-in MCP server, which
binds a port the IDE picks (`mcp <digest> <secret>` to the daemon). That endpoint
has to own that exact port rather than choose one, since the CLI builds the
server's URL itself as `http://localhost:<port>/stream`.

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

PREAMBLE_FORMAT = "JUNIE-RELAY 1 %s %s\n"
MCP_PREAMBLE_FORMAT = "JUNIE-RELAY 1 mcp %s %s\n"
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


def handle(conn, preamble, host, host_port):
    try:
        upstream = socket.create_connection((host, host_port), timeout=5)
    except OSError as err:
        conn.close()
        sys.stderr.write("junie-ide-relay-endpoint: %s:%d: %s\n" % (host, host_port, err))
        return
    try:
        upstream.sendall(preamble.encode())
    except OSError:
        conn.close()
        upstream.close()
        return
    upstream.settimeout(None)
    conn.settimeout(None)
    splice(conn, upstream)


def bind(port):
    """Listening sockets for `port`, one per available loopback family.

    The CLI builds the built-in MCP server's URL as `http://localhost:<port>`,
    which resolves to ::1 first, so a listener on IPv4 alone would answer every
    connection only after a refused IPv6 attempt. Either family may be taken --
    a host without IPv6, or a sandbox with something already on that port -- so
    a partial bind still serves, and only having neither is fatal.
    """
    listeners = []
    failure = None
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((address, port))
        except OSError as err:
            listener.close()
            failure = failure or err
            continue
        listener.listen(64)
        listeners.append(listener)
    if not listeners:
        raise failure
    return listeners


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True, help="loopback port to listen on")
    parser.add_argument("--digest", required=True, help="md5 of the project path this endpoint serves")
    parser.add_argument("--mcp", action="store_true",
                        help="serve the IDE's built-in MCP server rather than its RPC")
    parser.add_argument("--host", required=True, help="host the relay daemon runs on")
    parser.add_argument("--host-port", type=int, required=True, help="the relay daemon's port")
    parser.add_argument("--record", required=True,
                        help="file to write '<port> <pid>' to once listening; the "
                             "launcher polls it to tell a bound endpoint from one "
                             "whose port was already taken")
    args = parser.parse_args(argv)

    # Read from the environment rather than a flag, so the secret stays out of the
    # process list that `is_our_endpoint` and any local process can read.
    secret = os.environ.get("JUNIE_IDE_RELAY_SECRET", "")
    if not secret:
        sys.stderr.write("junie-ide-relay-endpoint: JUNIE_IDE_RELAY_SECRET is empty, "
                         "so the daemon would refuse every preamble\n")
        return 1
    try:
        listeners = bind(args.port)
    except OSError as err:
        sys.stderr.write("junie-ide-relay-endpoint: port %d: %s\n" % (args.port, err))
        return 1
    # Not named `handle`: that is the module-level connection handler above, and
    # a local of the same name would shadow it at the thread target below.
    with open(args.record, "w") as record:
        record.write("%d %d\n" % (args.port, os.getpid()))

    preamble = (MCP_PREAMBLE_FORMAT if args.mcp else PREAMBLE_FORMAT) % (args.digest, secret)
    while True:
        try:
            readable, _, _ = select.select(listeners, [], [])
        except OSError:
            continue
        for listener in readable:
            try:
                conn, _ = listener.accept()
            except OSError:
                continue
            threading.Thread(target=handle,
                             args=(conn, preamble, args.host, args.host_port),
                             daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
