"""Static-derived MQTT contract tests: fixed synthetic auth, fake IO and localhost only."""
import hashlib
import socket
import struct
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosscli.im_mqtt import (
    DISCONNECT, PINGREQ, PINGRESP, ImAuth, MQTTConnection, MQTTError,
    MQTTProtocolError, MQTTTimeout, Packet, TransportAckOnly, decode_connack,
    decode_packet, decode_puback, decode_publish, decode_remaining_length,
    decode_suback, decode_utf8, encode_connect, encode_puback, encode_publish,
    encode_remaining_length, encode_subscribe, encode_utf8, make_auth,
)

AUTH = ImAuth("synthetic-client", "42-0-1.4-14.180", "synthetic-password")


def test_auth_exact_md5_slice_timestamp_and_repr():
    stamp = 1700000000000
    auth = make_auth(42, 0, "14.180", "abc", "synthetic-secret", stamp)
    assert auth.client_id == "3cd24fb0d6963f7d"
    assert auth.username == "42-0-1.4-14.180"
    assert auth.password == hashlib.md5(b"synthetic-secret1700000000000").hexdigest()[8:24] + str(stamp)
    assert auth.password not in repr(auth)
    fallback = make_auth(42, 0, "14.180", "", "synthetic-secret", stamp)
    assert fallback.client_id == hashlib.md5(fallback.username.encode()).hexdigest()[8:24]


@pytest.mark.parametrize("changes", [dict(uid=0), dict(uid="-1"), dict(role=-1),
    dict(app_version=""), dict(secret_key=""), dict(device_id=42), dict(now_ms=-1), dict(now_ms=True)])
def test_bad_auth_inputs(changes):
    args = dict(uid=42, role=0, app_version="14.180", device_id="abc", secret_key="synthetic", now_ms=0)
    with pytest.raises(ValueError):
        make_auth(**{**args, **changes})


@pytest.mark.parametrize("value,encoded", [(0, b"\x00"), (127, b"\x7f"), (128, b"\x80\x01"),
    (16383, b"\xff\x7f"), (16384, b"\x80\x80\x01"), (268435455, b"\xff\xff\xff\x7f")])
def test_remaining_length_golden(value, encoded):
    assert encode_remaining_length(value) == encoded
    assert decode_remaining_length(encoded) == (value, len(encoded))
    assert decode_remaining_length(b"x" + encoded + b"y", 1) == (value, len(encoded) + 1)


@pytest.mark.parametrize("raw", [b"", b"\x80", b"\x80\x00", b"\xff\x00", b"\x80\x80\x80\x80\x01"])
def test_remaining_length_rejects_bad(raw):
    with pytest.raises(MQTTProtocolError):
        decode_remaining_length(raw)


@pytest.mark.parametrize("value", [-1, 268435456, True, 1.2])
def test_bad_remaining_length_inputs(value):
    with pytest.raises(ValueError):
        encode_remaining_length(value)


def test_utf8_exact_length_and_limits():
    assert encode_utf8("chat") == b"\x00\x04chat"
    assert encode_utf8("图") == b"\x00\x03\xe5\x9b\xbe"
    assert decode_utf8(b"\x00\x03\xe5\x9b\xbe!") == ("图", 5)
    for value in ("a\x00b", "x" * 65536, "\ud800"):
        with pytest.raises(ValueError):
            encode_utf8(value)
    for raw in (b"\x00", b"\x00\x02x", b"\x00\x01\xff", b"\x00\x01\x00"):
        with pytest.raises(MQTTProtocolError):
            decode_utf8(raw)


def test_connect_is_mqtt_31_not_311():
    raw = encode_connect(ImAuth("a", "b", "c"))
    assert raw == bytes.fromhex("101500064d514973647003c20168000161000162000163")
    assert decode_packet(raw).body[:12] == b"\x00\x06MQIsdp\x03\xc2\x01\x68"
    assert b"synthetic-password" not in repr(decode_packet(encode_connect(AUTH))).encode()


def test_publish_and_subscribe_are_plain_protobuf_qos_one():
    payload = b"\x08\x01\x12\x03abc"
    raw = encode_publish(payload, 0x1234)
    assert raw == b"\x32\x0f\x00\x04chat\x12\x34" + payload
    incoming = decode_publish(decode_packet(raw))
    assert (incoming.topic, incoming.sequence, incoming.qos, incoming.payload) == ("chat", 0x1234, 1, payload)
    assert encode_publish(payload, 9, duplicate=True)[0] == 0x3A
    assert encode_subscribe(7) == b"\x82\x09\x00\x07\x00\x04chat\x01"
    assert encode_puback(7) == b"\x40\x02\x00\x07"
    assert PINGREQ == b"\xc0\x00" and PINGRESP == b"\xd0\x00" and DISCONNECT == b"\xe0\x00"


@pytest.mark.parametrize("seq", [0, -1, 65536, True])
def test_publish_rejects_invalid_sequence(seq):
    with pytest.raises(ValueError):
        encode_publish(b"payload", seq)


@pytest.mark.parametrize("raw", [b"", b"\x40", b"\x40\x02\x00", b"\x40\x02\x00\x01extra",
    b"\x41\x00", b"\x00\x00", b"\xf0\x00", b"\x36\x00"])
def test_packet_length_and_header_validation(raw):
    with pytest.raises(MQTTProtocolError):
        decode_packet(raw)


def test_packet_cap_applies_before_body():
    with pytest.raises(MQTTProtocolError, match="cap"):
        decode_packet(b"\x30\x05abcde", max_packet_size=4)


def test_boss_puback_two_and_eighteen_byte_contract():
    short = decode_puback(decode_packet(b"\x40\x02\x12\x34"), expected_sequence=0x1234,
                          expected_client_mid=123)
    assert short.transport_only and short.client_mid is None and short.server_mid is None
    raw = b"\x40\x12" + struct.pack(">Hqq", 0x1234, 123, 456)
    ack = decode_puback(decode_packet(raw), expected_sequence=0x1234, expected_client_mid=123)
    assert not ack.transport_only and (ack.client_mid, ack.server_mid) == (123, 456)
    with pytest.raises(MQTTProtocolError, match="sequence"):
        decode_puback(decode_packet(raw), expected_sequence=1)
    with pytest.raises(MQTTProtocolError, match="client message"):
        decode_puback(decode_packet(raw), expected_client_mid=124)
    for cmid, mid in [(0, 1), (-1, 1), (1, 0), (1, -1)]:
        with pytest.raises(MQTTProtocolError):
            decode_puback(Packet(0x40, struct.pack(">Hqq", 1, cmid, mid)))


@pytest.mark.parametrize("size", [0, 1, 3, 10, 17, 19, 32])
def test_puback_does_not_accept_arbitrary_extension_lengths(size):
    with pytest.raises(MQTTProtocolError):
        decode_puback(Packet(0x40, b"\x01" * size))


def test_connack_and_suback_contracts():
    assert decode_connack(Packet(0x20, b"\x00\x00")) == (False, 0)
    assert decode_suback(Packet(0x90, b"\x00\x01\x01"), expected_sequence=1) == (1, (1,))
    for raw in (b"\x02\x00", b"\x00\x06", b"\x00"):
        with pytest.raises(MQTTProtocolError):
            decode_connack(Packet(0x20, raw))
    assert decode_suback(Packet(0x90, b"\x00\x01"), expected_sequence=1) == (1, ())
    for raw in (b"\x00\x00\x01", b"\x00\x01\x03", b"\x00"): 
        with pytest.raises(MQTTProtocolError):
            decode_suback(Packet(0x90, raw))


def read_wire(sock):
    def exact(size):
        result = bytearray()
        while len(result) < size:
            part = sock.recv(size - len(result))
            assert part, "synthetic client closed prematurely"
            result.extend(part)
        return bytes(result)
    first = exact(1)
    varint = bytearray()
    for _ in range(4):
        varint.extend(exact(1))
        if not varint[-1] & 128:
            break
    size, _ = decode_remaining_length(bytes(varint))
    return decode_packet(first + varint + exact(size))


@contextmanager
def local_peer(handler):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    errors = []
    def serve():
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(2)
                handler(conn)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    conn = socket.create_connection(listener.getsockname(), timeout=2)
    try:
        yield conn
    finally:
        conn.close()
        thread.join(3)
        listener.close()
        assert not thread.is_alive(), "synthetic server exceeded test deadline"
        if errors:
            raise errors[0]


def handshake(sock):
    assert read_wire(sock).to_bytes() == encode_connect(AUTH)
    # Fragmentation is intentional: the adapter must read exact packet bytes.
    for byte in b"\x20\x02\x00\x00":
        sock.sendall(bytes([byte]))


def test_localhost_full_flow_extended_ack_incoming_queue_and_heartbeat():
    def peer(sock):
        handshake(sock)
        subscription = read_wire(sock)
        assert subscription.first_byte == 0x82
        seq = subscription.body[:2]
        sock.sendall(Packet(0x90, seq + b"\x01").to_bytes())
        request = decode_publish(read_wire(sock))
        assert request.payload == b"synthetic-image-protobuf"
        sock.sendall(encode_publish(b"synthetic-type5-protobuf", 77))
        assert read_wire(sock).to_bytes() == encode_puback(77)
        sock.sendall(Packet(0x40, struct.pack(">Hqq", request.sequence, 123, 456)).to_bytes())
        assert read_wire(sock).to_bytes() == PINGREQ
        sock.sendall(PINGRESP)
        assert read_wire(sock).to_bytes() == DISCONNECT
    with local_peer(peer) as sock:
        c = MQTTConnection(sock, timeout=1)
        c.connect(AUTH)
        assert c.subscribe_chat() == 1
        assert c.publish(b"synthetic-image-protobuf", 123) == 456
        assert decode_publish(c.receive()).payload == b"synthetic-type5-protobuf"
        c.ping()
        c.disconnect()
        assert not c.authenticated


def test_localhost_short_ack_is_never_business_success():
    def peer(sock):
        handshake(sock)
        request = decode_publish(read_wire(sock))
        sock.sendall(encode_publish(b"synthetic-type5", 44) + encode_puback(request.sequence))
        assert read_wire(sock).to_bytes() == encode_puback(44)
    with local_peer(peer) as sock:
        c = MQTTConnection(sock, timeout=1)
        c.connect(AUTH)
        with pytest.raises(TransportAckOnly) as exc:
            c.publish(b"synthetic", 100)
        assert exc.value.ack.transport_only
        assert decode_publish(c.receive()).payload == b"synthetic-type5"
        c.close()


@pytest.mark.parametrize("delta,cmid,mid", [(1, 100, 200), (0, 101, 200), (0, 100, 0)])
def test_localhost_mismatch_never_reports_success(delta, cmid, mid):
    def peer(sock):
        handshake(sock)
        request = decode_publish(read_wire(sock))
        sock.sendall(Packet(0x40, struct.pack(">Hqq", request.sequence + delta, cmid, mid)).to_bytes())
    with local_peer(peer) as sock:
        c = MQTTConnection(sock, timeout=1)
        c.connect(AUTH)
        with pytest.raises(MQTTProtocolError):
            c.publish(b"synthetic", 100)
        c.close()


def test_no_default_network_factory_or_implicit_auth():
    with pytest.raises(ValueError, match="explicit"):
        MQTTConnection()
    with pytest.raises(ValueError, match="host"):
        MQTTConnection(socket_factory=lambda *_: None)
    for timeout in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError):
            MQTTConnection(object(), timeout=timeout)


class FakeSocket:
    def __init__(self, incoming=b""):
        self.incoming, self.sent, self.closed = incoming, [], False
    def settimeout(self, value):
        assert 0 < value <= 1
    def sendall(self, raw):
        self.sent.append(raw)
    def recv(self, size):
        if not self.incoming:
            raise socket.timeout()
        result, self.incoming = self.incoming[:size], self.incoming[size:]
        return result
    def close(self):
        self.closed = True


def test_fake_factory_and_tls_context_are_explicit_injected_and_preserved():
    sock = FakeSocket(b"\x20\x02\x00\x00")
    calls = []
    def factory(address, *, timeout):
        calls.append((address, timeout))
        return sock
    class Context:
        def wrap_socket(self, raw, *, server_hostname):
            assert raw is sock and server_hostname == "synthetic.invalid"
            return raw
    c = MQTTConnection(socket_factory=factory, host="synthetic.invalid", port=443,
                       ssl_context=Context(), timeout=1)
    c.connect(AUTH)
    assert len(calls) == 1 and calls[0][0] == ("synthetic.invalid", 443)
    assert sock.sent == [encode_connect(AUTH)]
    c.close()
    assert sock.closed


def test_fake_receive_timeout_closes_no_retry_and_no_false_success():
    sock = FakeSocket(b"\x20\x02\x00\x00")
    c = MQTTConnection(sock, timeout=1)
    c.connect(AUTH)
    with pytest.raises(MQTTTimeout):
        c.publish(b"synthetic", 123)
    assert sock.closed and not c.authenticated and len(sock.sent) == 2


def test_fake_oversize_packet_rejected_before_body_read():
    sock = FakeSocket(b"\x30\x05")
    c = MQTTConnection(sock, timeout=1, max_packet_size=4)
    with pytest.raises(MQTTProtocolError, match="cap"):
        c.receive()
    assert sock.closed


def test_fake_send_timeout_closes_without_retry():
    class TimeoutSocket(FakeSocket):
        def sendall(self, raw):
            self.sent.append(raw)
            raise socket.timeout()
    sock = TimeoutSocket()
    c = MQTTConnection(sock, timeout=1)
    with pytest.raises(MQTTTimeout):
        c.connect(AUTH)
    assert sock.closed and len(sock.sent) == 1


def test_outbound_local_size_cap_before_socket_send():
    sock = FakeSocket()
    c = MQTTConnection(sock, timeout=1, max_packet_size=4)
    with pytest.raises(MQTTProtocolError, match="cap"):
        c.send(encode_publish(b"synthetic", 1))
    assert not sock.sent
    c.close()


def test_fake_subscribe_rejection_and_connack_rejection():
    sock = FakeSocket(b"\x20\x02\x00\x05")
    c = MQTTConnection(sock, timeout=1)
    with pytest.raises(MQTTError, match="CONNECT"):
        c.connect(AUTH)
    assert sock.closed and not c.authenticated
    sock = FakeSocket(b"\x20\x02\x00\x00\x90\x03\x00\x01\x80")
    c = MQTTConnection(sock, timeout=1)
    c.connect(AUTH)
    with pytest.raises(MQTTProtocolError, match="subscription"):
        c.subscribe_chat()
    c.close()
