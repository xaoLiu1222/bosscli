"""Bounded, pure-Python single-image chat protobuf codec derived from APK 14.180.

No network or device dependencies. The schema was verified statically and one approved
live image workflow obtained a matching business message ID. A type-5 ACK must name the
exact client message id before success is reported.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

MAX_PROTO_BYTES = 2 * 1024 * 1024
MAX_FIELDS = 10000
LONG_MAX = (1 << 63) - 1


class ChatProtocolError(ValueError):
    """Malformed/inconsistent message, without echoing private payload or image URLs."""


def integer(value, label, low=0, high=LONG_MAX):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ChatProtocolError(f"invalid {label}")
    return value


def varint(value: int) -> bytes:
    integer(value, "varint", high=(1 << 64) - 1)
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(raw: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if pos >= len(raw):
            raise ChatProtocolError("truncated protobuf varint")
        char = raw[pos]; pos += 1
        if index == 9 and char > 1:
            raise ChatProtocolError("protobuf varint overflow")
        value |= (char & 127) << (index * 7)
        if not char & 128:
            return value, pos
    raise ChatProtocolError("invalid protobuf varint")


def number(tag: int, value: int) -> bytes:
    integer(tag, "field tag", 1, (1 << 29) - 1)
    # Java int32/int64 negatives are sign-extended varints (e.g. packed source with high bit).
    if isinstance(value, bool) or not isinstance(value, int) or not -(1 << 63) <= value <= LONG_MAX:
        raise ChatProtocolError("invalid signed integer")
    return varint(tag << 3) + varint(value & ((1 << 64) - 1))


def blob(tag: int, value: bytes) -> bytes:
    integer(tag, "field tag", 1, (1 << 29) - 1)
    if not isinstance(value, bytes) or len(value) > MAX_PROTO_BYTES:
        raise ChatProtocolError("invalid protobuf bytes")
    return varint((tag << 3) | 2) + varint(len(value)) + value


def text(tag: int, value: str) -> bytes:
    if not isinstance(value, str):
        raise ChatProtocolError("invalid protobuf text")
    return blob(tag, value.encode("utf-8"))


def fields(raw: bytes) -> dict[int, list[tuple[int, int | bytes]]]:
    if not isinstance(raw, bytes) or len(raw) > MAX_PROTO_BYTES:
        raise ChatProtocolError("protobuf size cap exceeded")
    pos, count = 0, 0
    out = {}
    while pos < len(raw):
        count += 1
        if count > MAX_FIELDS:
            raise ChatProtocolError("protobuf field count cap exceeded")
        key, pos = _read_varint(raw, pos)
        tag, wire = key >> 3, key & 7
        if not 1 <= tag < (1 << 29):
            raise ChatProtocolError("invalid protobuf field number")
        if wire == 0:
            value, pos = _read_varint(raw, pos)
        elif wire == 2:
            size, pos = _read_varint(raw, pos)
            if size > len(raw) - pos:
                raise ChatProtocolError("truncated protobuf bytes")
            value = raw[pos:pos + size]; pos += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if size > len(raw) - pos:
                raise ChatProtocolError("truncated fixed-width protobuf field")
            value = raw[pos:pos + size]; pos += size
        else:
            raise ChatProtocolError("unsupported protobuf wire type")
        out.setdefault(tag, []).append((wire, value))
    return out


def _single(parsed, tag: int, wire: int, *, required: bool = True):
    values = parsed.get(tag, [])
    if not values and not required:
        return None
    if len(values) != 1 or values[0][0] != wire:
        raise ChatProtocolError("ambiguous or missing protobuf field")
    return values[0][1]


@dataclass(frozen=True)
class ImageInfo:
    url: str = field(repr=False)
    width: int
    height: int

    def __post_init__(self):
        if not isinstance(self.url, str) or any(ord(c) < 32 for c in self.url):
            raise ChatProtocolError("invalid image URL")
        try:
            parsed = urlsplit(self.url)
            valid = parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
        except ValueError:
            valid = False
        if not valid:
            raise ChatProtocolError("image URL must be an HTTPS URL without userinfo")
        integer(self.width, "image width", 1, 100000)
        integer(self.height, "image height", 1, 100000)

    def encode(self):
        return text(1, self.url) + number(2, self.width) + number(3, self.height)


def encode_image_message(*, from_uid: int, to_uid: int, client_mid: int,
                         origin: ImageInfo, tiny: ImageInfo, from_source: int = 0,
                         to_source: int = 0, sender_name: str = "", now_ms: int | None = None) -> bytes:
    integer(from_uid, "sender uid", 1)
    integer(to_uid, "recipient uid", 1)
    integer(client_mid, "client message id", 1)
    if from_uid == to_uid:
        raise ChatProtocolError("sender and recipient must differ")
    for value in (from_source, to_source):
        integer(value, "user source", 0, 65535)
    stamp = int(time.time() * 1000) if now_ms is None else now_ms
    integer(stamp, "message timestamp", 1)
    if not isinstance(origin, ImageInfo) or not isinstance(tiny, ImageInfo):
        raise ChatProtocolError("both image descriptors are required")
    sender = number(1, from_uid)
    if sender_name:
        sender += text(2, sender_name)
    sender += number(7, from_source)
    recipient = number(1, to_uid) + number(7, to_source)
    image = number(1, 0) + blob(2, tiny.encode()) + blob(3, origin.encode())
    body = number(1, 3) + number(2, 1) + blob(5, image)
    # manager/i.s explicitly sets these default-valued optional fields too.
    message = (blob(1, sender) + blob(2, recipient) + number(3, 1) + number(4, client_mid)
               + number(5, stamp) + blob(6, body) + number(7, 0) + number(10, 0)
               + number(11, client_mid) + number(12, 2) + number(13, 0)
               + number(18, 0) + number(20, 0))
    return number(1, 1) + text(2, "1.4") + blob(3, message)


def business_ack(payload: bytes, expected_client_mid: int) -> int | None:
    """Return matching type-5 serverMid; unrelated messages are not confirmation.

    Reject duplicate/conflicting ids, invalid types and ambiguous ACK fields rather than
    guessing which result to use. Transport packet-id ACKs are handled by im_mqtt.
    """
    integer(expected_client_mid, "expected client message id", 1)
    root = fields(payload)
    if _single(root, 1, 0) != 5:
        return None
    result = None
    for wire, raw in root.get(7, []):
        if wire != 2:
            raise ChatProtocolError("invalid messageSync wire type")
        sync = fields(raw)
        client = _single(sync, 1, 0)
        server = _single(sync, 2, 0)
        if not 0 < client <= LONG_MAX or not 0 < server <= LONG_MAX:
            raise ChatProtocolError("invalid messageSync identifiers")
        if client == expected_client_mid:
            if result is not None:
                raise ChatProtocolError("duplicate messageSync confirmation")
            result = server
    return result
