"""Tests for the byte splice both relay halves carry."""
import socket
import threading
import unittest

from tests.scripts import endpoint, relayd

TIMEOUT = 5


class SpliceTest(unittest.TestCase):
    """The same cases over the daemon's copy and the endpoint's."""

    def copies(self):
        return (("daemon", relayd.splice), ("endpoint", endpoint.splice))

    def splice(self, splice, client, upstream):
        """Run `splice` in a thread, returning it and whatever it raised."""
        raised = []

        def run():
            try:
                splice(client, upstream)
            except BaseException as err:
                raised.append(err)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, raised

    def test_each_direction_carries_its_bytes(self):
        for label, splice in self.copies():
            with self.subTest(label):
                relay_client, client = socket.socketpair()
                relay_upstream, ide = socket.socketpair()
                for sock in (client, ide):
                    sock.settimeout(TIMEOUT)
                self.addCleanup(client.close)
                self.addCleanup(ide.close)

                thread, raised = self.splice(splice, relay_client, relay_upstream)
                client.sendall(b"to the IDE")
                self.assertEqual(ide.recv(65536), b"to the IDE")
                ide.sendall(b"to the client")
                self.assertEqual(client.recv(65536), b"to the client")

                # Both peers go: one direction's EOF alone is a half-close, and
                # the other stays open for the reply.
                client.close()
                ide.close()
                thread.join(TIMEOUT)
                self.assertFalse(thread.is_alive())
                self.assertEqual(raised, [])

    def test_a_peer_that_cannot_take_a_write_ends_the_splice_quietly(self):
        for label, splice in self.copies():
            with self.subTest(label):
                relay_client, client = socket.socketpair()
                relay_upstream, ide = socket.socketpair()
                client.settimeout(TIMEOUT)
                self.addCleanup(client.close)
                # The far end of the IDE socket is gone, so a write to it raises
                # EPIPE: the splice ends rather than raising out of the thread.
                ide.close()

                thread, raised = self.splice(splice, relay_client, relay_upstream)
                client.sendall(b"request")
                thread.join(TIMEOUT)
                self.assertFalse(thread.is_alive())
                self.assertEqual([type(err).__name__ for err in raised], [])

    def test_a_half_closed_client_still_receives_its_reply(self):
        for label, splice in self.copies():
            with self.subTest(label):
                relay_client, client = socket.socketpair()
                relay_upstream, ide = socket.socketpair()
                for sock in (client, ide):
                    sock.settimeout(TIMEOUT)
                self.addCleanup(client.close)
                self.addCleanup(ide.close)

                thread, raised = self.splice(splice, relay_client, relay_upstream)
                client.sendall(b"request")
                self.assertEqual(ide.recv(65536), b"request")
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(ide.recv(65536), b"")

                ide.sendall(b"reply")
                self.assertEqual(client.recv(65536), b"reply")
                ide.close()
                thread.join(TIMEOUT)
                self.assertFalse(thread.is_alive())
                self.assertEqual(raised, [])


if __name__ == "__main__":
    unittest.main()
