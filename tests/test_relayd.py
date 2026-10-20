"""Tests for the relay daemon's parsers, its tool allowlist and its exit status."""
import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest

from tests.scripts import relayd

ALLOW = ("get_*", "apply_patch", "execute_tool")
WITHHELD = "execute_terminal_command"
TIMEOUT = 5


class Reader:
    """An HTTP message reader over a socket, keeping the bytes it ran past."""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b""

    def fill(self):
        data = self.sock.recv(65536)
        if not data:
            raise EOFError("socket closed")
        self.buffer += data

    def read_until(self, marker):
        while marker not in self.buffer:
            self.fill()
        end = self.buffer.index(marker) + len(marker)
        data, self.buffer = self.buffer[:end], self.buffer[end:]
        return data

    def read_exact(self, count):
        while len(self.buffer) < count:
            self.fill()
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def quiet(self):
        """Whatever arrives in the next moment, or None when nothing does."""
        self.sock.settimeout(0.3)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(TIMEOUT)
        if not data:
            raise EOFError("socket closed")
        return data

    def read_chunk(self):
        """One chunk frame, with the framing it arrived in."""
        size = int(self.read_until(b"\r\n")[:-2].split(b";")[0], 16)
        return b"%x\r\n" % size + self.read_exact(size + 2)

    def read_message(self):
        """The next whole HTTP message, as (head, body)."""
        head = self.read_until(b"\r\n\r\n")
        headers = relayd.parse_head(head)[1]
        if b"chunked" in headers.get(b"transfer-encoding", b"").lower():
            body = b""
            while not body.endswith(b"0\r\n\r\n"):
                body += self.read_chunk()
            return head, body
        return head, self.read_exact(int(headers.get(b"content-length", b"0")))


class SpliceSession:
    """One MCP exchange over socket pairs, with a fake IDE on the far side."""

    def __init__(self, allow=ALLOW):
        self.relay_client, self.client = socket.socketpair()
        self.relay_upstream, self.ide = socket.socketpair()
        for sock in (self.client, self.ide):
            sock.settimeout(TIMEOUT)
        self.client_reader = Reader(self.client)
        self.ide_reader = Reader(self.ide)
        self.thread = threading.Thread(
            target=relayd.splice_mcp,
            args=(self.relay_client, self.relay_upstream, allow),
            daemon=True)
        self.thread.start()

    def send(self, body, chunked=False):
        """Write one request, returning the bytes the relay should forward."""
        head = (b"POST /mcp HTTP/1.1\r\nHost: relay\r\n"
                b"Content-Type: application/json\r\n")
        if chunked:
            raw = (head + b"Transfer-Encoding: chunked\r\n\r\n"
                   + b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n")
        else:
            raw = head + b"Content-Length: %d\r\n\r\n" % len(body) + body
        self.client.sendall(raw)
        return raw

    def forwarded(self):
        """The next message the IDE was sent, as its head and body joined."""
        head, body = self.ide_reader.read_message()
        return head + body

    def reply(self, payload):
        body = json.dumps(payload).encode()
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body))
        self.ide.sendall(head + body)

    def reply_to_client(self):
        """The next reply the client receives, as a parsed body."""
        _, body = self.client_reader.read_message()
        return json.loads(body)

    def ide_is_silent(self):
        """Whether the IDE is sent nothing in the next moment."""
        self.ide.settimeout(0.3)
        try:
            return not self.ide.recv(65536)
        except socket.timeout:
            return True
        finally:
            self.ide.settimeout(TIMEOUT)

    def close(self):
        for sock in (self.ide, self.client):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self.thread.join(TIMEOUT)


def call(name):
    """A `tools/call` request naming `name`, as its body."""
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name}}).encode()


class AllowlistTest(unittest.TestCase):
    def session(self, allow=ALLOW):
        session = SpliceSession(allow)
        self.addCleanup(session.close)
        return session

    def test_withheld_call_is_refused_and_never_forwarded(self):
        session = self.session()
        session.send(call(WITHHELD))
        refusal = session.reply_to_client()
        self.assertEqual(refusal["id"], 1)
        self.assertEqual(refusal["error"]["code"], -32601)
        self.assertIn(WITHHELD, refusal["error"]["message"])
        self.assertTrue(session.ide_is_silent())

    def test_withheld_notification_is_refused_with_a_null_id(self):
        session = self.session()
        session.send(json.dumps({"jsonrpc": "2.0", "method": "tools/call",
                                 "params": {"name": WITHHELD}}).encode())
        refusal = session.reply_to_client()
        self.assertIsNone(refusal["id"])
        self.assertEqual(refusal["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())

    def test_withheld_batch_is_refused_whole(self):
        session = self.session()
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": WITHHELD}}]
        session.send(json.dumps(batch).encode())
        refusal = session.reply_to_client()
        self.assertIsInstance(refusal, list)
        self.assertEqual(refusal[0]["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())

    def test_withheld_call_in_a_chunked_body_is_refused(self):
        session = self.session()
        session.send(call(WITHHELD), chunked=True)
        self.assertEqual(session.reply_to_client()["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())

    def test_allowed_call_is_forwarded_byte_for_byte(self):
        session = self.session()
        raw = session.send(call("get_project_modules"))
        self.assertEqual(session.forwarded(), raw)

    def test_unreadable_body_is_answered_rather_than_forwarded(self):
        session = self.session()
        session.send(b"not json at all")
        head, body = session.client_reader.read_message()
        self.assertIn(b"400", head)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)
        self.assertTrue(session.ide_is_silent())

    def test_withheld_tool_is_removed_from_a_tools_list_reply(self):
        session = self.session()
        session.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list"}).encode())
        session.reply({"jsonrpc": "2.0", "id": 1, "result": {"tools": [
            {"name": "get_project_modules"}, {"name": WITHHELD}]}})
        names = [tool["name"] for tool in session.reply_to_client()["result"]["tools"]]
        self.assertEqual(names, ["get_project_modules"])

    def test_filter_off_forwards_everything(self):
        session = self.session(allow=None)
        raw = session.send(call(WITHHELD))
        self.assertEqual(session.forwarded(), raw)

    def test_empty_allowlist_refuses_every_call(self):
        session = self.session(allow=())
        session.send(call("get_project_modules"))
        self.assertEqual(session.reply_to_client()["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())


class StreamTest(unittest.TestCase):
    HEAD = (b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n")

    def session(self):
        session = SpliceSession()
        self.addCleanup(session.close)
        raw = session.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                       "method": "tools/list"}).encode())
        self.assertEqual(session.forwarded(), raw)
        session.ide.sendall(self.HEAD)
        self.assertEqual(session.client_reader.read_until(b"\r\n\r\n"), self.HEAD)
        return session

    def test_stream_terminator_ends_the_body_and_the_reply_after_it_is_filtered(self):
        session = self.session()
        event = b"data: x\n\n"
        session.ide.sendall(b"%x\r\n" % len(event) + event + b"\r\n")
        self.assertEqual(session.client_reader.read_chunk(),
                         b"%x\r\n" % len(event) + event + b"\r\n")
        session.ide.sendall(b"0\r\n\r\n")
        self.assertEqual(session.client_reader.read_chunk(), b"0\r\n\r\n")
        session.reply({"jsonrpc": "2.0", "id": 1, "result": {"tools": [
            {"name": "get_project_modules"}, {"name": WITHHELD}]}})
        names = [tool["name"] for tool in session.reply_to_client()["result"]["tools"]]
        self.assertEqual(names, ["get_project_modules"])

    def test_a_refusal_waits_for_the_stream_body_and_then_arrives(self):
        session = self.session()
        event = b"data: x\n\n"
        session.ide.sendall(b"%x\r\n" % len(event) + event + b"\r\n")
        session.client_reader.read_chunk()
        session.send(call(WITHHELD))
        self.assertTrue(session.ide_is_silent())
        # Nothing may be written into the body: a reply there is read as a chunk
        # size, and the stream breaks.
        self.assertIsNone(session.client_reader.quiet())
        session.ide.sendall(b"0\r\n\r\n")
        self.assertEqual(session.client_reader.read_chunk(), b"0\r\n\r\n")
        self.assertEqual(session.reply_to_client()["error"]["code"], -32601)

    def test_the_stream_survives_a_refusal_and_carries_its_events(self):
        session = self.session()
        event = b"data: x\n\n"
        session.ide.sendall(b"%x\r\n" % len(event) + event + b"\r\n")
        session.client_reader.read_chunk()
        session.send(call(WITHHELD))
        second = b"data: y\n\n"
        session.ide.sendall(b"%x\r\n" % len(second) + second + b"\r\n")
        self.assertEqual(session.client_reader.read_chunk(),
                         b"%x\r\n" % len(second) + second + b"\r\n")
        session.ide.sendall(b"0\r\n\r\n")
        self.assertEqual(session.client_reader.read_chunk(), b"0\r\n\r\n")
        self.assertEqual(session.reply_to_client()["error"]["code"], -32601)

    def test_a_flood_of_refusals_ends_the_session_rather_than_queueing_for_ever(self):
        session = self.session()
        event = b"data: x\n\n"
        session.ide.sendall(b"%x\r\n" % len(event) + event + b"\r\n")
        session.client_reader.read_chunk()
        # Past the cap the queue stops growing, and the session ends: the client
        # that flooded a body it was not reading is not owed an answer.
        try:
            for _ in range(relayd.OWED_LIMIT + 2):
                session.send(call(WITHHELD))
        except BrokenPipeError:
            pass
        session.client.settimeout(2.0)
        self.assertEqual(session.client.recv(65536), b"")


class ParserTest(unittest.TestCase):
    def test_allowed_tool_matches_exact_and_wildcard_patterns(self):
        allow = ("apply_patch", "get_*")
        self.assertTrue(relayd.allowed_tool("apply_patch", allow))
        self.assertTrue(relayd.allowed_tool("get_project_modules", allow))
        self.assertFalse(relayd.allowed_tool("apply_patch_all", allow))
        self.assertFalse(relayd.allowed_tool(WITHHELD, allow))
        self.assertFalse(relayd.allowed_tool(None, allow))

    def test_refused_tool_names_the_call_outside_the_list(self):
        self.assertEqual(relayd.refused_tool({"name": WITHHELD}, ALLOW), WITHHELD)
        self.assertIsNone(relayd.refused_tool({"name": "get_project_modules"}, ALLOW))

    def test_refused_tool_reads_the_dispatcher_target(self):
        dispatcher = {"name": "execute_tool",
                      "arguments": {"command": "get_project_modules x"}}
        self.assertIsNone(relayd.refused_tool(dispatcher, ALLOW))
        dispatcher["arguments"] = {"command": WITHHELD + " ls"}
        self.assertEqual(relayd.refused_tool(dispatcher, ALLOW), WITHHELD)

    def test_refused_tool_refuses_a_call_it_cannot_read(self):
        self.assertEqual(relayd.refused_tool([], ALLOW), relayd.DISPATCHER_TOOL)
        self.assertEqual(relayd.refused_tool({}, ALLOW), relayd.DISPATCHER_TOOL)
        self.assertEqual(relayd.refused_tool({"name": "execute_tool"}, ALLOW),
                         relayd.DISPATCHER_TOOL)
        self.assertEqual(relayd.refused_tool(
            {"name": "execute_tool", "arguments": {}}, ALLOW), relayd.DISPATCHER_TOOL)

    def test_scrub_payload_removes_withheld_tools_and_listing_names(self):
        payload = {"result": {"tools": [{"name": "get_a"}, {"name": WITHHELD}],
                              "content": [{"text": relayd.TOOL_LIST_MARKER
                                           + "get_a, " + WITHHELD}]}}
        self.assertTrue(relayd.scrub_payload(payload, ALLOW))
        self.assertEqual([tool["name"] for tool in payload["result"]["tools"]], ["get_a"])
        self.assertEqual(payload["result"]["content"][0]["text"],
                         relayd.TOOL_LIST_MARKER + "get_a")

    def test_scrub_payload_leaves_an_allowlisted_result_alone(self):
        self.assertFalse(relayd.scrub_payload({"result": {"tools": [{"name": "get_a"}]}},
                                              ALLOW))

    def test_filtered_response_reframes_a_scrubbed_body(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "result": {"tools": [{"name": WITHHELD}]}}).encode()
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body))
        new_head, new_body = relayd.filtered_response(head, body, ALLOW)
        self.assertEqual(relayd.parse_head(new_head)[1][b"content-length"],
                         b"%d" % len(new_body))
        self.assertEqual(json.loads(new_body)["result"]["tools"], [])

    def test_filtered_response_passes_a_non_json_reply(self):
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 2\r\n\r\n")
        self.assertIsNone(relayd.filtered_response(head, b"hi", ALLOW))

    def test_rebuild_head_replaces_the_framing(self):
        head = (b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n")
        self.assertEqual(relayd.rebuild_head(head, 4),
                         b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n")

    def test_decode_chunked_unframes_a_body(self):
        self.assertEqual(relayd.decode_chunked(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"),
                         b"Wikipedia")
        self.assertIsNone(relayd.decode_chunked(b"zz\r\nWiki\r\n0\r\n\r\n"))

    def test_read_chunked_keeps_the_framing(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        framed = b"4\r\nWiki\r\n0\r\n\r\n"
        far.sendall(framed)
        self.assertEqual(relayd.HttpConnection(near).read_chunked(), framed)

    def test_read_message_unframes_and_decodes(self):
        body = b'{"jsonrpc":"2.0","id":1}'
        framed = b"%x\r\n" % len(body) + body + b"\r\n0\r\n\r\n"
        head = b"POST /mcp HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"
        self.assertEqual(relayd.read_message(head, framed), {"jsonrpc": "2.0", "id": 1})
        plain = b"POST /mcp HTTP/1.1\r\nContent-Length: 2\r\n\r\n"
        self.assertEqual(relayd.read_message(plain, b""), {})
        self.assertIsNone(relayd.read_message(plain, b"{"))

    def test_tool_calls_reads_a_message_and_a_batch(self):
        called = {"method": "tools/call", "params": {"name": WITHHELD}}
        self.assertEqual(relayd.tool_calls(called), [called])
        self.assertEqual(relayd.tool_calls([{"method": "tools/list"}, called]), [called])
        self.assertEqual(relayd.tool_calls([None, 3]), [])
        self.assertEqual(relayd.tool_calls("nope"), [])

    def test_split_digest_secret_reads_a_preamble_payload(self):
        digest = "a" * 32
        secret = "0" * 64
        self.assertEqual(relayd.split_digest_secret((digest + " " + secret).encode()),
                         (digest, secret.encode()))
        self.assertEqual(relayd.split_digest_secret((digest + secret).encode()),
                         (None, None))
        self.assertEqual(relayd.split_digest_secret(("a" * 31 + " " + secret).encode()),
                         (None, None))
        self.assertEqual(relayd.split_digest_secret((digest + " " + "0" * 63).encode()),
                         (None, None))
        self.assertEqual(relayd.split_digest_secret((digest + " " + "z" * 64).encode()),
                         (None, None))

    def test_hex_predicates_reject_the_wrong_length_and_alphabet(self):
        self.assertTrue(relayd.is_digest(b"0123456789abcdef" * 2))
        self.assertFalse(relayd.is_digest(b"0123456789abcdef"))
        self.assertTrue(relayd.is_secret(b"0123456789abcdef" * 4))
        self.assertFalse(relayd.is_secret(b"0123456789abcdef" * 3 + b"g"))


class ExitStatusTest(unittest.TestCase):
    """A refused start is a setting, and exits with a status that says so."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.instances = os.path.join(self.root, "instances")
        self.secrets = os.path.join(self.root, "relay-secrets")
        self.config = os.path.join(self.root, "relayd.toml")
        os.mkdir(self.instances)
        os.mkdir(self.secrets, 0o700)

    def start(self):
        """`main`'s status, with its message kept off the test's output.

        The config path is named rather than left to the environment, so the
        caller's own `relayd.toml` neither changes the answer nor fails to load.
        """
        with contextlib.redirect_stderr(io.StringIO()):
            return relayd.main(["--instances", self.instances,
                                "--secrets", self.secrets,
                                "--config", self.config])

    def test_a_config_that_will_not_load_is_a_settings_failure(self):
        with open(self.config, "w") as handle:
            handle.write('allow_tools = "nope"\n')
        self.assertEqual(self.start(), relayd.EX_CONFIG)

    def test_an_exposed_directory_is_a_settings_failure(self):
        os.chmod(self.secrets, 0o755)
        self.assertEqual(self.start(), relayd.EX_CONFIG)

    def test_an_exposed_record_is_a_settings_failure(self):
        record = os.path.join(self.secrets, "project.json")
        with open(record, "w") as handle:
            json.dump({"project_path": "/p", "secret": "0" * 64}, handle)
        os.chmod(record, 0o644)
        self.assertEqual(self.start(), relayd.EX_CONFIG)

    def test_a_missing_instance_directory_is_not(self):
        os.rmdir(self.instances)
        self.assertEqual(self.start(), 1)


if __name__ == "__main__":
    unittest.main()
