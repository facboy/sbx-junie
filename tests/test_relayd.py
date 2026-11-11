"""Tests for the relay daemon's parsers, its tool allowlist and its exit status."""
import contextlib
import hashlib
import http.server
import io
import json
import os
import pathlib
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

    def __init__(self, allow=ALLOW, root=None, methods=None):
        self.relay_client, self.client = socket.socketpair()
        self.relay_upstream, self.ide = socket.socketpair()
        for sock in (self.client, self.ide):
            sock.settimeout(TIMEOUT)
        self.client_reader = Reader(self.client)
        self.ide_reader = Reader(self.ide)
        self.thread = threading.Thread(
            target=relayd.splice_mcp,
            args=(self.relay_client, self.relay_upstream, allow, root, methods),
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

    def test_a_refusal_naming_a_quoted_tool_is_valid_json(self):
        # The name is whatever the client sent, so it can close a JSON string
        # built by formatting it in. The reply has to parse either way.
        session = self.session()
        name = 'execute"tool\\x'
        session.send(call(name))
        self.assertEqual(session.reply_to_client()["error"]["message"],
                         "Tool '%s' is not available" % name)
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

    def test_a_batched_reply_is_scrubbed_member_by_member(self):
        session = self.session()
        session.send(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                 {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]).encode())
        session.reply([{"jsonrpc": "2.0", "id": 1, "result": {"tools": [
                           {"name": "get_project_modules"}, {"name": WITHHELD}]}},
                       {"jsonrpc": "2.0", "id": 2, "result": {"tools": [
                           {"name": WITHHELD}]}}])
        head, body = session.client_reader.read_message()
        replies = json.loads(body)
        self.assertNotIn(WITHHELD.encode(), body)
        self.assertEqual([tool["name"] for tool in replies[0]["result"]["tools"]],
                         ["get_project_modules"])
        self.assertEqual(replies[1]["result"]["tools"], [])
        # The body was re-framed, so the head still describes it.
        self.assertEqual(relayd.parse_head(head)[1][b"content-length"],
                         b"%d" % len(body))

    def test_a_batch_inside_the_allowlist_is_forwarded_untouched(self):
        session = self.session()
        session.send(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]).encode())
        session.forwarded()
        body = json.dumps([{"jsonrpc": "2.0", "id": 1,
                            "result": {"tools": [{"name": "get_project_modules"}]}}]).encode()
        session.ide.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                            b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.assertEqual(session.client_reader.read_message()[1], body)

    def test_accept_encoding_is_dropped_on_the_way_to_the_ide(self):
        session = self.session()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        session.client.sendall(b"POST /mcp HTTP/1.1\r\nHost: relay\r\n"
                               b"Content-Type: application/json\r\n"
                               b"Accept-Encoding: gzip, deflate\r\n"
                               b"Content-Length: %d\r\n\r\n" % len(body) + body)
        forwarded = session.forwarded()
        self.assertNotIn(b"Accept-Encoding", forwarded)
        self.assertTrue(forwarded.endswith(body))

    def test_a_length_delimited_event_stream_is_followed_by_a_filtered_reply(self):
        session = self.session()
        event = b"data: tick\n\n"
        session.ide.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                            b"Content-Length: %d\r\n\r\n" % len(event) + event)
        self.assertEqual(session.client_reader.read_message()[1], event)
        session.reply({"jsonrpc": "2.0", "id": 2, "result": {"tools": [
            {"name": "get_project_modules"}, {"name": WITHHELD}]}})
        names = [tool["name"] for tool in session.reply_to_client()["result"]["tools"]]
        self.assertEqual(names, ["get_project_modules"])

    def test_a_reply_the_filter_cannot_read_ends_the_session(self):
        session = self.session()
        session.send(json.dumps({"jsonrpc": "2.0", "id": 1,
                                 "method": "tools/list"}).encode())
        session.forwarded()
        # The daemon says why on stderr; the test keeps that off its own output.
        with contextlib.redirect_stderr(io.StringIO()):
            session.ide.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                                b"Content-Encoding: gzip\r\nContent-Length: 3\r\n\r\n"
                                b"\x1f\x8b\x08")
            session.client.settimeout(2.0)
            self.assertEqual(session.client.recv(65536), b"")
        session.thread.join(TIMEOUT)
        self.assertFalse(session.thread.is_alive())

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
        reply = relayd.filtered_response(head, body, ALLOW)
        new_head, new_body = reply.split(b"\r\n\r\n", 1)
        self.assertEqual(relayd.parse_head(new_head)[1][b"content-length"],
                         b"%d" % len(new_body))
        self.assertEqual(json.loads(new_body)["result"]["tools"], [])

    def test_filtered_response_passes_a_reply_it_has_nothing_to_scrub_in(self):
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                b"Content-Length: 2\r\n\r\n")
        self.assertEqual(relayd.filtered_response(head, b"hi", ALLOW), head + b"hi")

    def test_filtered_response_refuses_a_json_body_it_cannot_parse(self):
        # A reply the daemon cannot read names no tool it can check, so it is not
        # passed on: a listing inside it would reach the client unfiltered.
        head = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Encoding: gzip\r\nContent-Length: 3\r\n\r\n")
        self.assertIsNone(relayd.filtered_response(head, b"\x1f\x8b\x08", ALLOW))

    def test_rebuild_head_replaces_the_framing(self):
        head = (b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n")
        self.assertEqual(relayd.rebuild_head(head, 4),
                         b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\n")

    def test_decode_chunked_unframes_a_body(self):
        self.assertEqual(relayd.decode_chunked(b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"),
                         b"Wikipedia")
        self.assertIsNone(relayd.decode_chunked(b"zz\r\nWiki\r\n0\r\n\r\n"))

    def test_chunk_size_takes_hex_digits_and_nothing_int_would_also_accept(self):
        self.assertEqual(relayd.chunk_size(b"4"), 4)
        self.assertEqual(relayd.chunk_size(b"1f"), 31)
        self.assertEqual(relayd.chunk_size(b"4;ext=1"), 4)
        # `int(line, 16)` takes these too, which no chunk frames with.
        self.assertIsNone(relayd.chunk_size(b"0x4"))
        self.assertIsNone(relayd.chunk_size(b" 4"))
        self.assertIsNone(relayd.chunk_size(b""))

    def test_a_head_naming_its_framing_twice_is_a_conflict(self):
        self.assertFalse(relayd.framing_conflict(
            b"POST /mcp HTTP/1.1\r\nContent-Length: 2\r\n\r\n"))
        self.assertFalse(relayd.framing_conflict(
            b"POST /mcp HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n"))
        self.assertTrue(relayd.framing_conflict(
            b"POST /mcp HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 5\r\n\r\n"))
        self.assertTrue(relayd.framing_conflict(
            b"POST /mcp HTTP/1.1\r\ncontent-length: 2\r\nContent-Length: 5\r\n\r\n"))

    def test_a_head_with_no_boundary_gives_up_at_the_cap(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        conn = relayd.HttpConnection(near)
        # Bytes already read past the cap, with no boundary among them: the head
        # is not a message, so it is the end of the connection rather than growth.
        conn.buffer = b"X-Pad: " + b"a" * (relayd.MAX_HEAD + 1)
        self.assertIsNone(conn.read_head())

    def test_a_declared_length_past_the_cap_is_not_buffered(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        conn = relayd.HttpConnection(near)
        self.assertIsNone(conn.read_exact(relayd.MAX_MESSAGE + 1))
        self.assertIsNone(conn.read_exact(-1))

    def test_a_chunked_body_with_no_end_stops_at_the_cap(self):
        near, far = socket.socketpair()
        self.addCleanup(near.close)
        self.addCleanup(far.close)
        conn = relayd.HttpConnection(near)
        frame = b"10000\r\n" + b"a" * 0x10000 + b"\r\n"
        # A megabyte per round, all of it short of the cap together only after
        # several: the reader must stop rather than grow without limit.
        conn.buffer = frame * (relayd.MAX_MESSAGE // len(frame) + 2)
        self.assertIsNone(conn.read_chunked())

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


class PairBridgeTest(unittest.TestCase):
    """`pair <digest> <secret>` reaches the plugin socket the instance file names.

    Schema 3 replaced the per-file `rpc_token` with a socket the CLI dials for
    one, and that path is on the host: published as it stands, the sandbox is
    handed an address nothing there can dial, which the CLI reports as a pairing
    failure rather than a missing IDE.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.instances = os.path.join(self.root, "instances")
        self.secrets = os.path.join(self.root, "relay-secrets")
        os.mkdir(self.instances)
        os.mkdir(self.secrets, 0o700)
        self.secret = "a" * relayd.SECRET_HEX_LEN
        self.project = os.path.join(self.root, "project")
        self.digest = hashlib.md5(self.project.encode()).hexdigest()
        record = os.path.join(self.secrets, "project.json")
        with open(record, "w") as handle:
            json.dump({"project_path": self.project, "secret": self.secret}, handle)
        os.chmod(record, 0o600)

    def publish(self, **fields):
        """One instance file naming this project, written `fields` ahead."""
        info = {"schema_version": 3, "project_path": self.project,
                "port": 41154, "pid": 1}
        info.update(fields)
        path = os.path.join(self.instances, "%s_%d.json" % (self.digest, info["pid"]))
        with open(path, "w") as handle:
            json.dump(info, handle)
        return path

    def test_the_instance_file_pair_socket_is_resolved(self):
        pair = os.path.join(self.root, "pair.sock")
        self.publish(pair_socket=pair)
        self.assertEqual(relayd.pair_socket_for_digest(self.instances, self.digest), pair)

    def test_an_instance_file_with_no_pair_socket_resolves_to_none(self):
        # A plugin below schema 3 publishes a token in the file instead, and the
        # CLI falls back to it, so this is a working configuration rather than a
        # fault.
        self.publish(rpc_token="t")
        self.assertIsNone(relayd.pair_socket_for_digest(self.instances, self.digest))

    def test_a_pair_preamble_is_spliced_to_the_plugin_socket(self):
        pair = os.path.join(self.root, "pair.sock")
        self.publish(pair_socket=pair)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(pair)
        listener.listen(1)
        self.addCleanup(listener.close)
        seen = {}

        def fake_plugin():
            conn, _ = listener.accept()
            seen["hello"] = conn.recv(4096)
            conn.sendall(b'{"status":"allowed","token":"tok"}\n')
            conn.close()

        plugin = threading.Thread(target=fake_plugin, daemon=True)
        plugin.start()

        relay_end, client = socket.socketpair()
        self.addCleanup(client.close)
        allow = relayd.AllowState(os.path.join(self.root, "none.toml"), None, False)
        threading.Thread(target=relayd.serve,
                         args=(relay_end, self.instances, self.secrets, allow),
                         daemon=True).start()
        client.settimeout(TIMEOUT)
        client.sendall(b"JUNIE-RELAY 1 pair %s %s\n"
                       % (self.digest.encode(), self.secret.encode()))
        client.sendall(b'{"client":"junie"}\n')
        self.assertEqual(client.recv(4096), b'{"status":"allowed","token":"tok"}\n')
        plugin.join(TIMEOUT)
        self.assertEqual(seen.get("hello"), b'{"client":"junie"}\n')

    def test_a_pair_preamble_without_the_secret_reaches_no_plugin(self):
        pair = os.path.join(self.root, "pair.sock")
        self.publish(pair_socket=pair)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(pair)
        listener.listen(1)
        listener.settimeout(0.5)
        self.addCleanup(listener.close)

        relay_end, client = socket.socketpair()
        self.addCleanup(client.close)
        allow = relayd.AllowState(os.path.join(self.root, "none.toml"), None, False)
        threading.Thread(target=relayd.serve,
                         args=(relay_end, self.instances, self.secrets, allow),
                         daemon=True).start()
        client.settimeout(TIMEOUT)
        client.sendall(b"JUNIE-RELAY 1 pair %s %s\n"
                       % (self.digest.encode(), ("b" * relayd.SECRET_HEX_LEN).encode()))
        self.assertEqual(client.recv(4096), b"")
        with self.assertRaises(socket.timeout):
            listener.accept()


class PairTokenTest(unittest.TestCase):
    """The MCP port is resolved on a plugin that publishes no token in its file.

    Schema 3 moved the token out of the instance file and onto the pair socket, so
    a daemon reading only `rpc_token` cannot authenticate the credentials call and
    answers `unknown`. The mirror then never starts, and the CLI reports an MCP
    setup failure.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        self.instances = os.path.join(self.root, "instances")
        os.mkdir(self.instances)
        self.project = os.path.join(self.root, "project")
        self.digest = hashlib.md5(self.project.encode()).hexdigest()

        # A plugin that issues a token over its pair socket.
        self.pair = os.path.join(self.root, "pair.sock")
        self.plugin = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.plugin.bind(self.pair)
        self.plugin.listen(8)
        self.addCleanup(self.plugin.close)
        threading.Thread(target=self.serve_pair, daemon=True).start()

        # The IDE's RPC, which the credentials call authenticates against.
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length))
                requests.append((self.headers.get("Authorization"), body))
                result = ({"port": 54321} if self.headers.get("Authorization") == "tok-abc"
                          else {"error": "unauthorized"})
                out = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()
                self.send_response(200)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *_args):
                pass

        requests = self.requests
        self.rpc = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.rpc.server_close)
        threading.Thread(target=self.rpc.serve_forever, daemon=True).start()

    def serve_pair(self):
        while True:
            try:
                conn, _ = self.plugin.accept()
            except OSError:
                return
            try:
                conn.recv(4096)
                conn.sendall(b'{"status":"allowed","token":"tok-abc"}\n')
            finally:
                conn.close()

    def publish(self, **fields):
        info = {"schema_version": 3, "project_path": self.project,
                "pid": 1, "port": self.rpc.server_address[1]}
        info.update(fields)
        with open(os.path.join(self.instances, "%s_%d.json" % (self.digest, info["pid"])), "w") as handle:
            json.dump(info, handle)

    def test_a_schema_3_file_resolves_the_port_over_the_pair_socket(self):
        relayd.PAIR_TOKENS.clear()
        self.publish(pair_socket=self.pair)
        self.assertEqual(relayd.mcp_port_for_digest(self.instances, self.digest),
                         ("port", 54321))
        self.assertEqual(self.requests[0][0], "tok-abc")
        self.assertEqual(self.requests[0][1]["method"], relayd.MCP_CREDENTIALS_METHOD)

    def test_a_file_token_still_resolves_the_port(self):
        # Schema 2 and below publish the token itself, and that still works.
        relayd.PAIR_TOKENS.clear()
        self.publish(schema_version=2, rpc_token="tok-abc")
        self.assertEqual(relayd.mcp_port_for_digest(self.instances, self.digest),
                         ("port", 54321))

    def test_a_plugin_that_issues_no_token_leaves_the_port_unknown(self):
        relayd.PAIR_TOKENS.clear()
        self.publish(pair_socket=os.path.join(self.root, "absent.sock"))
        self.assertEqual(relayd.mcp_port_for_digest(self.instances, self.digest),
                         ("unknown", None))


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


def rpc_call(method):
    """A JSON-RPC request naming `method`, as the RPC channel's body."""
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": {}}).encode()


def path_call(name, **arguments):
    """A `tools/call` naming `name` with `arguments`, as its body."""
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}}).encode()


# The gate is reached only by a tool the allowlist already covers, so these tests
# allow every tool they drive and let the path argument be what is refused.
GATE_ALLOW = ("*",)


class PathGateTest(unittest.TestCase):
    """A path argument that resolves outside the project never reaches the IDE.

    The project is agent-writable, so a symlink in it is the way out: the IDE's
    containment check compares the name it was handed, and the daemon resolves
    where that name lands before forwarding the call.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.project = base / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src" / "inside.py").write_text("x = 1\n")
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_text("host-only\n")
        # The escape itself: a project entry naming a directory outside it.
        (self.project / "escape").symlink_to(self.outside)
        # An in-project symlink that stays in the project, as node_modules/.bin
        # does, which has to keep working.
        (self.project / "alias").symlink_to(self.project / "src")

    def session(self):
        session = SpliceSession(GATE_ALLOW, str(self.project))
        self.addCleanup(session.close)
        return session

    def assert_refused(self, session, argument):
        refusal = session.reply_to_client()
        self.assertEqual(refusal["error"]["code"], -32602)
        self.assertIn(argument, refusal["error"]["message"])
        self.assertTrue(session.ide_is_silent())

    def test_a_path_inside_the_project_is_forwarded(self):
        session = self.session()
        raw = session.send(path_call("read_file", file_path=str(self.project / "src")))
        self.assertEqual(session.forwarded(), raw)

    def test_a_relative_path_inside_the_project_is_forwarded(self):
        session = self.session()
        raw = session.send(path_call("read_file", file_path="src/inside.py"))
        self.assertEqual(session.forwarded(), raw)

    def test_a_project_symlink_to_an_outside_directory_is_refused(self):
        session = self.session()
        session.send(path_call("read_file", file_path=str(self.project / "escape")
                               + "/secret"))
        self.assert_refused(session, "file_path")

    def test_a_plain_outside_path_is_refused(self):
        session = self.session()
        session.send(path_call("read_file", file_path=str(self.outside / "secret")))
        self.assert_refused(session, "file_path")

    def test_a_traversing_path_is_refused(self):
        session = self.session()
        session.send(path_call("read_file", file_path="../../etc/passwd"))
        self.assert_refused(session, "file_path")

    def test_an_in_project_symlink_still_works(self):
        session = self.session()
        raw = session.send(path_call("read_file", file_path=str(self.project / "alias")))
        self.assertEqual(session.forwarded(), raw)

    def test_each_gated_tool_is_checked(self):
        for tool, argument in (("list_directory_tree", "directoryPath"),
                               ("create_new_file", "pathInProject"),
                               ("open_file_in_editor", "filePath"),
                               ("search_text", "paths")):
            with self.subTest(tool=tool):
                session = self.session()
                session.send(path_call(tool, **{argument: str(self.project / "escape")}))
                self.assert_refused(session, argument)

    def test_a_list_of_paths_is_each_checked(self):
        session = self.session()
        session.send(path_call("search_text", paths=[str(self.project / "src"),
                                                     str(self.outside)]))
        self.assert_refused(session, "paths")

    def test_a_non_string_path_is_refused_rather_than_forwarded(self):
        session = self.session()
        session.send(path_call("read_file", file_path={"nested": "value"}))
        self.assert_refused(session, "file_path")

    def test_an_omitted_optional_path_is_forwarded(self):
        session = self.session()
        raw = session.send(path_call("get_run_configurations"))
        self.assertEqual(session.forwarded(), raw)

    def test_a_dispatcher_call_naming_an_escaping_path_is_refused(self):
        session = self.session()
        session.send(path_call(
            "execute_tool",
            command="read_file --file_path %s" % (self.project / "escape" / "secret")))
        self.assert_refused(session, "file_path")

    def test_a_dispatcher_call_naming_an_inside_path_is_forwarded(self):
        session = self.session()
        raw = session.send(path_call(
            "execute_tool",
            command="read_file --file_path %s" % (self.project / "src")))
        self.assertEqual(session.forwarded(), raw)

    def test_an_escaping_call_in_a_batch_is_refused_whole(self):
        session = self.session()
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                  "params": {"name": "read_file",
                             "arguments": {"file_path": str(self.outside)}}}]
        session.send(json.dumps(batch).encode())
        refusal = session.reply_to_client()
        self.assertEqual(refusal[0]["error"]["code"], -32602)
        self.assertTrue(session.ide_is_silent())

    def test_the_gate_is_skipped_when_no_project_resolves(self):
        session = SpliceSession(GATE_ALLOW, None)
        self.addCleanup(session.close)
        raw = session.send(path_call("read_file", file_path=str(self.outside)))
        self.assertEqual(session.forwarded(), raw)


class PatchGateTest(unittest.TestCase):
    """A patch whose body names a path outside the project never reaches the IDE.

    `apply_patch` declares only `projectPath`, so the only place its targets show
    is the body: a gate that trusted the schema would forward a patch adding
    `../../.ssh/authorized_keys`.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        self.project = base / "project"
        (self.project / "src").mkdir(parents=True)
        self.outside = base / "outside"
        self.outside.mkdir()

    def session(self):
        session = SpliceSession(GATE_ALLOW, str(self.project))
        self.addCleanup(session.close)
        return session

    def assert_refused(self, session):
        refusal = session.reply_to_client()
        self.assertEqual(refusal["error"]["code"], -32602)
        self.assertTrue(session.ide_is_silent())
        return refusal

    def test_an_apply_patch_body_inside_the_project_is_forwarded(self):
        body = ("*** Begin Patch\n*** Add File: src/new.py\n+x = 1\n"
                "*** End Patch")
        session = self.session()
        raw = session.send(path_call("apply_patch", input=body))
        self.assertEqual(session.forwarded(), raw)

    def test_an_apply_patch_body_that_adds_an_outside_file_is_refused(self):
        body = ("*** Begin Patch\n*** Add File: ../escape.txt\n+x\n"
                "*** End Patch")
        session = self.session()
        session.send(path_call("apply_patch", input=body))
        self.assertIn("escape.txt", self.assert_refused(session)["error"]["message"])

    def test_an_apply_patch_body_that_deletes_an_outside_file_is_refused(self):
        body = ("*** Begin Patch\n*** Delete File: %s\n*** End Patch"
                % (self.outside / "gone.txt"))
        session = self.session()
        session.send(path_call("apply_patch", input=body))
        self.assert_refused(session)

    def test_a_unified_diff_that_touches_an_outside_file_is_refused(self):
        body = ("--- a/%s/x.txt\n+++ b/%s/x.txt\n@@ -0,0 +1 @@\n+x\n"
                % (self.outside, self.outside))
        session = self.session()
        session.send(path_call("apply_patch", input=body))
        self.assert_refused(session)

    def test_a_unified_diff_inside_the_project_is_forwarded(self):
        body = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        session = self.session()
        raw = session.send(path_call("apply_patch", patch=body))
        self.assertEqual(session.forwarded(), raw)

    def test_a_multi_file_patch_with_one_escaping_target_is_refused_whole(self):
        body = ("*** Begin Patch\n*** Add File: src/ok.py\n+x\n"
                "*** Add File: ../escape.txt\n+y\n*** End Patch")
        session = self.session()
        session.send(path_call("apply_patch", input=body))
        # One escaping target refuses the call, so the inside one is not run.
        self.assert_refused(session)
        self.assertTrue(session.ide_is_silent())

    def test_a_patch_whose_header_names_no_path_is_refused(self):
        body = "*** Begin Patch\n*** Add File:\n+x\n*** End Patch"
        session = self.session()
        session.send(path_call("apply_patch", input=body))
        self.assertIn(relayd.PATCH_UNREADABLE,
                      self.assert_refused(session)["error"]["message"])

    def test_a_patch_through_the_dispatcher_is_gated_too(self):
        body = "*** Begin Patch\n*** Add File: ../escape.txt\n+x\n*** End Patch"
        session = self.session()
        session.send(path_call("execute_tool",
                               command='apply_patch --input %s' % json.dumps(body)))
        self.assert_refused(session)

    def test_a_patch_naming_no_file_is_forwarded(self):
        # Nothing to resolve is not the same as something that escapes: the IDE
        # answers an empty patch itself.
        session = self.session()
        raw = session.send(path_call("apply_patch", input="*** Begin Patch\n*** End Patch"))
        self.assertEqual(session.forwarded(), raw)


class RpcAllowTest(unittest.TestCase):
    """The RPC channel refuses a method the allowlist does not name.

    The channel is JSON-RPC over HTTP like the MCP one, but it carries no tool
    names: the method is the whole surface, and `JunieEditService.applyPatch` is a
    project write that no tool-level gate reaches.
    """

    def session(self, methods=relayd.DEFAULT_ALLOW_RPC):
        session = SpliceSession(None, None, methods)
        self.addCleanup(session.close)
        return session

    def test_an_allowlisted_method_is_forwarded(self):
        session = self.session()
        raw = session.send(rpc_call("JunieMcpService.getBuiltInMcpServerCredentials"))
        self.assertEqual(session.forwarded(), raw)

    def test_the_initialize_handshake_is_forwarded(self):
        # It returns the service list, the project id and the protocol version,
        # and the CLI calls it once per connection: refusing it breaks the
        # channel rather than narrowing it.
        session = self.session()
        raw = session.send(rpc_call("initialize"))
        self.assertEqual(session.forwarded(), raw)

    def test_apply_patch_is_refused_and_never_forwarded(self):
        session = self.session()
        session.send(rpc_call("JunieEditService.applyPatch"))
        refusal = session.reply_to_client()
        self.assertEqual(refusal["error"]["code"], -32601)
        self.assertIn("JunieEditService.applyPatch", refusal["error"]["message"])
        self.assertTrue(session.ide_is_silent())

    def test_an_unknown_method_is_refused(self):
        session = self.session()
        session.send(rpc_call("JunieUnknownService.noSuchMethod"))
        self.assertEqual(session.reply_to_client()["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())

    def test_a_refused_method_in_a_batch_is_refused_whole(self):
        session = self.session()
        batch = [{"jsonrpc": "2.0", "id": 1,
                  "method": "JunieMcpService.getBuiltInMcpServerCredentials"},
                 {"jsonrpc": "2.0", "id": 2, "method": "JunieBuildService.buildProject"}]
        session.send(json.dumps(batch).encode())
        refusal = session.reply_to_client()
        self.assertEqual(refusal[0]["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())

    def test_a_request_naming_no_method_is_refused(self):
        session = self.session()
        session.send(json.dumps({"jsonrpc": "2.0", "id": 1}).encode())
        self.assertEqual(session.reply_to_client()["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())

    def test_the_method_failure_builds_a_valid_body(self):
        # The method is whatever the client sent, so a quote in it must not break
        # the body the CLI has to parse.
        session = self.session()
        session.send(rpc_call('Junie"EditService.applyPatch'))
        self.assertIn("is not available",
                      session.reply_to_client()["error"]["message"])

    def test_a_chunked_request_is_filtered_too(self):
        session = self.session()
        session.send(rpc_call("JunieEditService.applyPatch"), chunked=True)
        self.assertEqual(session.reply_to_client()["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())

    def test_an_empty_allowlist_refuses_every_method(self):
        session = self.session(methods=())
        session.send(rpc_call("JunieMcpService.getBuiltInMcpServerCredentials"))
        self.assertEqual(session.reply_to_client()["error"]["code"], -32601)
        self.assertTrue(session.ide_is_silent())


if __name__ == "__main__":
    unittest.main()


# The `tools/list` surface the path gate is drawn against, as the property names
# each tool carries. Pinned rather than fetched, so a tool a later IDE build adds
# is a decision here rather than a silent gap in the gate.
PATH_TOOL_SURFACE = {
    "read_file": {"file_path"},
    "create_new_file": {"pathInProject"},
    "list_directory_tree": {"directoryPath"},
    "open_file_in_editor": {"filePath"},
    "get_file_problems": {"filePath"},
    "get_symbol_info": {"filePath"},
    "get_python_environment": {"filePath"},
    "get_run_configurations": {"filePath"},
    "analyze_calls": {"treePath"},
    "search_file": {"paths"},
    "search_regex": {"paths"},
    "search_symbol": {"paths"},
    "search_text": {"paths"},
    "rename_refactoring": {"pathInProject"},
    "create_notebook": {"file_path"},
    "read_notebook": {"file_path"},
    "read_notebook_cell": {"file_path"},
    "edit_notebook": {"file_path"},
    "get_notebook_state": {"file_path"},
    "git_status": {"repositoryPathRelativeToProject"},
}
# `apply_patch` carries its targets in a body, so it is gated by `patch_paths`.
# Every other tool on the surface names no path but `projectPath`, which is the
# project itself and needs no check.
BODY_GATED_TOOLS = {"apply_patch"}
PROJECT_PATH_ONLY = {
    "execute_tool", "cancel_sql_query", "create_database_connection",
    "edit_database_connection", "fetch_query_result", "get_database_object_description",
    "introspect_schema", "list_database_connections", "list_database_schemas",
    "list_recent_sql_queries", "list_schema_object_kinds", "list_schema_objects",
    "preview_table_data", "test_database_connection", "list_data_extractors",
    "get_project_dependencies", "get_project_modules", "get_all_open_file_paths",
    "get_repositories", "create_changelist", "get_changelists",
}


class ToolCoverageTest(unittest.TestCase):
    """Every tool that carries a path is either gated or explicitly exempt.

    A tool the map misses passes ungated, so the omission has to fail here rather
    than in production.
    """

    def test_every_path_bearing_tool_is_gated(self):
        for tool, parameters in PATH_TOOL_SURFACE.items():
            with self.subTest(tool=tool):
                self.assertIn(tool, relayd.TOOL_PATH_ARGS)
                self.assertEqual(set(relayd.TOOL_PATH_ARGS[tool]), parameters)

    def test_the_mapped_tools_are_the_ones_the_surface_names(self):
        self.assertEqual(set(relayd.TOOL_PATH_ARGS), set(PATH_TOOL_SURFACE))

    def test_a_tool_with_no_path_parameter_is_not_mapped(self):
        overlap = set(relayd.TOOL_PATH_ARGS) & PROJECT_PATH_ONLY
        self.assertEqual(set(), overlap)

    def test_a_body_gated_tool_is_not_also_mapped_by_parameter(self):
        self.assertEqual(set(), set(relayd.TOOL_PATH_ARGS) & BODY_GATED_TOOLS)
        self.assertIn(relayd.PATCH_TOOL, BODY_GATED_TOOLS)
