"""Tests for the sandbox-side relay endpoint."""
import os
import shutil
import socket
import tempfile
import unittest

from tests.scripts import endpoint


def has_ipv6():
    """Whether this kernel can make a socket of the IPv6 family at all."""
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
    except OSError:
        return False
    return True


class BindTest(unittest.TestCase):
    def test_every_loopback_family_the_kernel_has_is_bound(self):
        listeners = endpoint.bind(0)
        self.addCleanup(lambda: [listener.close() for listener in listeners])
        families = [listener.family for listener in listeners]
        self.assertIn(socket.AF_INET, families)
        if has_ipv6():
            self.assertIn(socket.AF_INET6, families)

    def test_a_family_the_kernel_cannot_create_still_serves_on_the_other(self):
        # A kernel without IPv6 raises on the socket call itself, before there is
        # anything to bind: the endpoint has an IPv4 listener and must use it.
        real = socket.socket
        made = []

        def fake(family, *args, **kwargs):
            if family == socket.AF_INET6:
                raise OSError(97, "Address family not supported by protocol")
            made.append(real(family, *args, **kwargs))
            return made[-1]
        self.addCleanup(lambda: [sock.close() for sock in made])

        endpoint.socket.socket = fake
        try:
            listeners = endpoint.bind(0)
        finally:
            endpoint.socket.socket = real
        self.assertEqual([listener.family for listener in listeners], [socket.AF_INET])

    def test_neither_family_available_raises(self):
        def fake(*_args, **_kwargs):
            raise OSError(97, "Address family not supported by protocol")

        real = endpoint.socket.socket
        endpoint.socket.socket = fake
        try:
            with self.assertRaises(OSError):
                endpoint.bind(0)
        finally:
            endpoint.socket.socket = real

    def test_a_unix_listener_replaces_the_file_a_dead_endpoint_left(self):
        # The launcher starts one endpoint per project, having checked that none is
        # live, so a socket file a killed endpoint left must not block the bind.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        path = os.path.join(directory, "pair.sock")
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(path)
        stale.close()

        listeners = endpoint.bind_unix(path)
        self.addCleanup(lambda: [listener.close() for listener in listeners])
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(client.close)
        client.connect(path)
        connected, _ = listeners[0].accept()
        connected.close()


if __name__ == "__main__":
    unittest.main()
