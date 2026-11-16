"""Offline-derived BOSS legacy IM MQTT 3.1 codec and explicit socket adapter.

Derived statically from APK 14.180, classes9.dex Lxh0/b and the bundled, modified
Paho wire classes. Subsequently verified in one explicitly approved live workflow
(auth/subscription/heartbeat and image business ACK); integrated via ImageSender.
This low-level module has no session files, account storage, network defaults, automatic
retries, automatic reconnects or background threads. The caller supplies all auth
inputs and either an already-connected socket or an explicit socket factory and
host/port. TLS uses a caller-supplied SSLContext; no APK private keys are embedded.

CONNECT uses MQIsdp level 3, clean session, 360-second keepalive; chat publishes
use QoS 1. A BOSS extended PUBACK body is >Hqq: wire packet-id, client-message-id,
server-message-id. A standard two-byte PUBACK is transport-only and MUST NOT be
reported as business success. TransportAckOnly exposes that distinction; callers
may then consume queued PUBLISH payloads and validate their protobuf type-5 ACK.
The adapter does not decode protobuf. Any incoming QoS-1 PUBLISH is transport-
acknowledged and queued while a command waits. There is one in-flight command,
no concurrent use, and no automatic retry after timeout (delivery is uncertain).

The service-local MMSMessage sequence is NOT necessarily the wire packet-id:
Paho MqttPublish does not copy MqttMessage.id and ClientState assigns its own id.
HTTP fallback and QoS 2 are deliberately unsupported here. See work/im_transport.
"""
from __future__ import annotations

import hashlib
import math
import socket
import ssl
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

MAX_REMAINING_LENGTH = 268435455
DEFAULT_MAX_PACKET_SIZE = 4 * 1024 * 1024  # local cap, not a measured server limit


class MQTTError(RuntimeError):
    """Credential-free transport/protocol error."""


class MQTTProtocolError(MQTTError, ValueError):
    pass


class MQTTTimeout(MQTTError, TimeoutError):
    """No final ACK before deadline; sending may already have happened."""


@dataclass(frozen=True)
class ImAuth:
    client_id: str
    username: str
    password: str = field(repr=False)


def make_auth(uid: int | str, role: int | str, app_version: str, device_id: str | None,
              secret_key: str, now_ms: int | None = None) -> ImAuth:
    """Pure derivation; device_id is App's persisted IMEI_CODE UUID, not hardware IMEI."""
    if not str(uid).isascii() or not str(uid).isdecimal() or int(uid) <= 0:
        raise ValueError("uid must be a positive decimal identifier")
    if not str(role).isascii() or not str(role).isdecimal():
        raise ValueError("role must be a nonnegative decimal value")
    if not isinstance(app_version, str) or not app_version or "\x00" in app_version:
        raise ValueError("app_version is required")
    if not isinstance(secret_key, str) or not secret_key:
        raise ValueError("secret_key is required")
    if device_id is not None and not isinstance(device_id, str):
        raise ValueError("device_id must be text")
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0:
        raise ValueError("now_ms must be a nonnegative integer")
    username = f"{int(uid)}-{int(role)}-1.4-{app_version}"
    client_id = hashlib.md5((device_id or username).encode("utf-8")).hexdigest()[8:24]
    timestamp = str(stamp)
    password = hashlib.md5((secret_key + timestamp).encode("utf-8")).hexdigest()[8:24] + timestamp
    return ImAuth(client_id, username, password)


def _integer(value: int, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"invalid {label}")
    return value


def encode_remaining_length(value: int) -> bytes:
    _integer(value, 0, MAX_REMAINING_LENGTH, "remaining length")
    result = bytearray()
    while True:
        digit = value % 128
        value //= 128
        result.append(digit | (0x80 if value else 0))
        if not value:
            return bytes(result)


def decode_remaining_length(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Return (length, next_offset); reject truncated, overlong and noncanonical encodings."""
    _integer(offset, 0, len(data), "offset")
    result = 0
    start = offset
    for shift in range(0, 28, 7):
        if offset == len(data):
            raise MQTTProtocolError("truncated remaining length")
        digit = data[offset]
        offset += 1
        result |= (digit & 127) << shift
        if not digit & 128:
            if data[start:offset] != encode_remaining_length(result):
                raise MQTTProtocolError("noncanonical remaining length")
            return result, offset
    raise MQTTProtocolError("remaining length exceeds four bytes")


def encode_utf8(value: str) -> bytes:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("invalid MQTT text")
    try:
        raw = value.encode("utf-8")
    except UnicodeError:
        raise ValueError("invalid MQTT text") from None
    if len(raw) > 65535:
        raise ValueError("MQTT text exceeds 65535 bytes")
    return struct.pack(">H", len(raw)) + raw


def decode_utf8(data: bytes, offset: int = 0) -> tuple[str, int]:
    _integer(offset, 0, len(data), "offset")
    if len(data) - offset < 2:
        raise MQTTProtocolError("truncated MQTT text length")
    length = struct.unpack_from(">H", data, offset)[0]
    end = offset + 2 + length
    if end > len(data):
        raise MQTTProtocolError("truncated MQTT text")
    try:
        text = data[offset + 2:end].decode("utf-8")
    except UnicodeError:
        raise MQTTProtocolError("invalid MQTT UTF-8") from None
    if "\x00" in text:
        raise MQTTProtocolError("NUL in MQTT text")
    return text, end


def _validate_header(first_byte: int) -> None:
    _integer(first_byte, 0, 255, "fixed header")
    kind, flags = first_byte >> 4, first_byte & 15
    if not 1 <= kind <= 14:
        raise MQTTProtocolError("unsupported MQTT packet type")
    if kind == 3:
        if (flags >> 1) & 3 == 3:
            raise MQTTProtocolError("invalid PUBLISH QoS")
    elif flags != (2 if kind in (6, 8, 10) else 0):
        raise MQTTProtocolError("invalid MQTT fixed-header flags")


@dataclass(frozen=True)
class Packet:
    first_byte: int
    body: bytes = field(repr=False)

    def __post_init__(self):
        _validate_header(self.first_byte)
        if not isinstance(self.body, bytes):
            raise TypeError("packet body must be bytes")

    @property
    def packet_type(self) -> int:
        return self.first_byte >> 4

    @property
    def flags(self) -> int:
        return self.first_byte & 15

    def to_bytes(self) -> bytes:
        return bytes([self.first_byte]) + encode_remaining_length(len(self.body)) + self.body


def decode_packet(data: bytes, *, max_packet_size: int = DEFAULT_MAX_PACKET_SIZE) -> Packet:
    if len(data) < 2:
        raise MQTTProtocolError("truncated MQTT packet")
    _validate_header(data[0])
    size, start = decode_remaining_length(data, 1)
    if size > max_packet_size:
        raise MQTTProtocolError("MQTT packet exceeds local size cap")
    if len(data) != start + size:
        raise MQTTProtocolError("MQTT packet length mismatch")
    return Packet(data[0], data[start:])


def encode_connect(auth: ImAuth, *, keepalive: int = 360) -> bytes:
    _integer(keepalive, 0, 65535, "keepalive")
    if not isinstance(auth, ImAuth) or not auth.client_id or not auth.username or not auth.password:
        raise ValueError("complete ImAuth is required")
    variable = encode_utf8("MQIsdp") + b"\x03\xc2" + struct.pack(">H", keepalive)
    payload = encode_utf8(auth.client_id) + encode_utf8(auth.username) + encode_utf8(auth.password)
    return Packet(0x10, variable + payload).to_bytes()


def encode_publish(payload: bytes, sequence: int, *, topic: str = "chat", duplicate: bool = False) -> bytes:
    _integer(sequence, 1, 65535, "packet sequence")
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if not topic or "+" in topic or "#" in topic:
        raise ValueError("invalid publish topic")
    return Packet(0x3A if duplicate else 0x32,
                  encode_utf8(topic) + struct.pack(">H", sequence) + payload).to_bytes()


def encode_subscribe(sequence: int, *, topic: str = "chat", qos: int = 1) -> bytes:
    _integer(sequence, 1, 65535, "packet sequence")
    _integer(qos, 0, 2, "subscription QoS")
    if not topic:
        raise ValueError("subscription topic is required")
    return Packet(0x82, struct.pack(">H", sequence) + encode_utf8(topic) + bytes([qos])).to_bytes()


def encode_puback(sequence: int) -> bytes:
    _integer(sequence, 1, 65535, "packet sequence")
    return Packet(0x40, struct.pack(">H", sequence)).to_bytes()


PINGREQ = b"\xc0\x00"
PINGRESP = b"\xd0\x00"
DISCONNECT = b"\xe0\x00"


@dataclass(frozen=True)
class PubAck:
    sequence: int
    client_mid: int | None = None
    server_mid: int | None = None

    @property
    def transport_only(self) -> bool:
        return self.client_mid is None


class TransportAckOnly(MQTTError):
    def __init__(self, ack: PubAck):
        self.ack = ack
        super().__init__("transport PUBACK has no business message IDs; validate protobuf ACK separately")


def decode_puback(packet: Packet, *, expected_sequence: int | None = None,
                  expected_client_mid: int | None = None) -> PubAck:
    if packet.first_byte != 0x40 or len(packet.body) not in (2, 18):
        raise MQTTProtocolError("PUBACK must have exactly 2 or 18 body bytes")
    sequence = struct.unpack_from(">H", packet.body)[0]
    if sequence == 0 or (expected_sequence is not None and sequence != expected_sequence):
        raise MQTTProtocolError("PUBACK sequence mismatch")
    if len(packet.body) == 2:
        return PubAck(sequence)
    _, client_mid, server_mid = struct.unpack(">Hqq", packet.body)
    if client_mid <= 0 or server_mid <= 0:
        raise MQTTProtocolError("PUBACK business message IDs must be positive")
    if expected_client_mid is not None and client_mid != expected_client_mid:
        raise MQTTProtocolError("PUBACK client message ID mismatch")
    return PubAck(sequence, client_mid, server_mid)


def decode_connack(packet: Packet) -> tuple[bool, int]:
    if packet.first_byte != 0x20 or len(packet.body) != 2 or packet.body[0] & ~1 or packet.body[1] > 5:
        raise MQTTProtocolError("malformed CONNACK")
    return bool(packet.body[0]), packet.body[1]


def decode_suback(packet: Packet, *, expected_sequence: int | None = None) -> tuple[int, tuple[int, ...]]:
    if packet.first_byte != 0x90 or len(packet.body) < 2:
        raise MQTTProtocolError("malformed SUBACK")
    sequence = struct.unpack_from(">H", packet.body)[0]
    granted = tuple(packet.body[2:])
    if sequence == 0 or (expected_sequence is not None and sequence != expected_sequence):
        raise MQTTProtocolError("SUBACK sequence mismatch")
    if any(value not in (0, 1, 2, 128) for value in granted):
        raise MQTTProtocolError("invalid SUBACK grant")
    return sequence, granted


@dataclass(frozen=True)
class IncomingPublish:
    topic: str
    payload: bytes = field(repr=False)
    qos: int = 0
    sequence: int | None = None


def decode_publish(packet: Packet) -> IncomingPublish:
    if packet.packet_type != 3:
        raise MQTTProtocolError("not a PUBLISH")
    qos = (packet.flags >> 1) & 3
    topic, offset = decode_utf8(packet.body)
    if not topic or "+" in topic or "#" in topic:
        raise MQTTProtocolError("invalid incoming PUBLISH topic")
    sequence = None
    if qos:
        if len(packet.body) - offset < 2:
            raise MQTTProtocolError("truncated PUBLISH sequence")
        sequence = struct.unpack_from(">H", packet.body, offset)[0]
        offset += 2
        if not sequence:
            raise MQTTProtocolError("zero PUBLISH sequence")
    return IncomingPublish(topic, packet.body[offset:], qos, sequence)


class MQTTConnection:
    """Explicit, synchronous socket adapter, never instantiated by existing business code.

    socket_factory, if used, is called as factory((host, port), timeout=seconds),
    like socket.create_connection. No factory is installed by default. The socket
    is owned/closed by this object. TLS wraps only sockets created by the factory;
    an injected socket is already configured. A timeout means uncertain delivery.
    """

    def __init__(self, sock: socket.socket | None = None, *, socket_factory: Callable | None = None,
                 host: str | None = None, port: int | None = None,
                 ssl_context: ssl.SSLContext | None = None, timeout: float = 10,
                 max_packet_size: int = DEFAULT_MAX_PACKET_SIZE):
        self._duration(timeout)
        _integer(max_packet_size, 1, MAX_REMAINING_LENGTH, "packet size cap")
        if sock is not None and (socket_factory is not None or ssl_context is not None):
            raise ValueError("injected socket must already be connected and TLS-configured")
        if sock is None and socket_factory is None:
            raise ValueError("an explicit connected socket or socket_factory is required")
        if sock is None:
            if not isinstance(host, str) or not host:
                raise ValueError("explicit host is required")
            _integer(port, 1, 65535, "port")
        self._socket = sock
        self._factory, self.host, self.port = socket_factory, host, port
        self._ssl_context = ssl_context
        self.timeout, self.max_packet_size = timeout, max_packet_size
        self._sequence = 0
        self._pending: deque[Packet] = deque()
        self.authenticated = False

    @staticmethod
    def _duration(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("timeout must be finite and positive")
        return float(value)

    def _deadline(self, timeout: float | None) -> float:
        return time.monotonic() + self._duration(self.timeout if timeout is None else timeout)

    @staticmethod
    def _left(deadline: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise MQTTTimeout("MQTT deadline expired; delivery is not confirmed")
        return left

    def next_sequence(self) -> int:
        self._sequence = self._sequence % 65535 + 1
        return self._sequence

    def _send(self, raw: bytes, deadline: float):
        decode_packet(raw, max_packet_size=self.max_packet_size)
        if self._socket is None:
            raise MQTTError("socket is not open")
        try:
            self._socket.settimeout(self._left(deadline))
            self._socket.sendall(raw)
        except (TimeoutError, socket.timeout):
            self.close()
            raise MQTTTimeout("MQTT send timed out; delivery is not confirmed") from None
        except OSError:
            self.close()
            raise

    def send(self, packet: Packet | bytes, *, timeout: float | None = None):
        raw = packet.to_bytes() if isinstance(packet, Packet) else packet
        decode_packet(raw, max_packet_size=self.max_packet_size)
        self._send(raw, self._deadline(timeout))

    def _read(self, size: int, deadline: float) -> bytes:
        if self._socket is None:
            raise MQTTError("socket is not open")
        parts = bytearray()
        while len(parts) < size:
            try:
                self._socket.settimeout(self._left(deadline))
                piece = self._socket.recv(size - len(parts))
            except (TimeoutError, socket.timeout):
                raise MQTTTimeout("MQTT receive timed out; delivery is not confirmed") from None
            if not piece:
                raise MQTTError("socket closed before a complete MQTT packet")
            parts.extend(piece)
        return bytes(parts)

    def _receive(self, deadline: float) -> Packet:
        # Partial reads cannot safely be resumed after a deadline; close on failure.
        try:
            first = self._read(1, deadline)[0]
            _validate_header(first)
            encoded = bytearray()
            for _ in range(4):
                encoded.extend(self._read(1, deadline))
                if not encoded[-1] & 128:
                    break
            size, _ = decode_remaining_length(bytes(encoded))
            if size > self.max_packet_size:
                raise MQTTProtocolError("MQTT packet exceeds local size cap")
            return Packet(first, self._read(size, deadline))
        except (MQTTError, OSError):
            self.close()
            raise

    def pop_pending(self) -> Packet | None:
        """Consume an already-received packet only; never reads/retries network I/O."""
        return self._pending.popleft() if self._pending else None

    def receive(self, *, timeout: float | None = None) -> Packet:
        """Return queued then wire packets; QoS-1 PUBLISH gets a transport PUBACK."""
        if self._pending:
            return self._pending.popleft()
        deadline = self._deadline(timeout)
        packet = self._receive(deadline)
        self._ack_incoming(packet, deadline)
        return packet

    def _ack_incoming(self, packet: Packet, deadline: float):
        if packet.packet_type == 3:
            message = decode_publish(packet)
            if message.qos == 2:
                raise MQTTProtocolError("incoming QoS 2 is not supported")
            if message.qos == 1:
                self._send(encode_puback(message.sequence), deadline)

    def _wait(self, packet_type: int, deadline: float) -> Packet:
        for _ in range(128):  # queue/count cap plus total wall-clock deadline
            packet = self._receive(deadline)
            if packet.packet_type == packet_type:
                return packet
            if packet.packet_type == 3:
                self._ack_incoming(packet, deadline)
                if len(self._pending) >= 128:
                    raise MQTTProtocolError("incoming PUBLISH queue cap exceeded")
                self._pending.append(packet)
            elif packet.first_byte == 0xD0 and not packet.body:
                continue
            else:
                raise MQTTProtocolError("unexpected packet while waiting for ACK")
        raise MQTTProtocolError("packet count cap exceeded while waiting for ACK")

    def connect(self, auth: ImAuth, *, timeout: float | None = None):
        raw = encode_connect(auth)
        if self.authenticated:
            raise MQTTError("connection is already authenticated")
        deadline = self._deadline(timeout)
        if self._socket is None:
            created = self._factory((self.host, self.port), timeout=self._left(deadline))
            self._socket = created
            try:
                if self._ssl_context is not None:
                    created.settimeout(self._left(deadline))
                    self._socket = self._ssl_context.wrap_socket(created, server_hostname=self.host)
            except Exception:
                self.close()
                raise
        self._send(raw, deadline)
        present, code = decode_connack(self._wait(2, deadline))
        if code or present:  # cleanSession=True cannot resume a prior session
            self.close()
            raise MQTTError(f"CONNECT rejected or inconsistent clean session (code {code})")
        self.authenticated = True

    def subscribe_chat(self, *, timeout: float | None = None) -> int | None:
        if not self.authenticated:
            raise MQTTError("CONNECT is required before SUBSCRIBE")
        deadline = self._deadline(timeout)
        sequence = self.next_sequence()
        self._send(encode_subscribe(sequence), deadline)
        _, granted = decode_suback(self._wait(9, deadline), expected_sequence=sequence)
        # Live BOSS server returned SUBACK body=packet-id only (90 02 00 01).
        # App's MqttSuback parser permits an empty grantedQos array. A matching id
        # confirms this response, but the server did not state a QoS: return None.
        if not granted:
            return None
        if len(granted) != 1 or granted[0] not in (0, 1):
            raise MQTTProtocolError("chat subscription was rejected or granted unexpected QoS")
        return granted[0]

    def publish(self, payload: bytes, expected_client_mid: int, *, timeout: float | None = None,
                sequence: int | None = None) -> int:
        if not self.authenticated:
            raise MQTTError("CONNECT is required before PUBLISH")
        _integer(expected_client_mid, 1, (1 << 63) - 1, "expected client message ID")
        seq = self.next_sequence() if sequence is None else sequence
        raw = encode_publish(payload, seq)
        deadline = self._deadline(timeout)
        self._send(raw, deadline)
        ack = decode_puback(self._wait(4, deadline), expected_sequence=seq,
                            expected_client_mid=expected_client_mid)
        if ack.transport_only:
            raise TransportAckOnly(ack)
        return ack.server_mid

    def ping(self, *, timeout: float | None = None):
        if not self.authenticated:
            raise MQTTError("CONNECT is required before PING")
        deadline = self._deadline(timeout)
        self._send(PINGREQ, deadline)
        if self._wait(13, deadline).body:
            raise MQTTProtocolError("PINGRESP body must be empty")

    def disconnect(self, *, timeout: float | None = None):
        try:
            if self._socket is not None and self.authenticated:
                self._send(DISCONNECT, self._deadline(timeout))
        finally:
            self.close()

    def close(self):
        sock, self._socket = self._socket, None
        self.authenticated = False
        if sock is not None:
            sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
