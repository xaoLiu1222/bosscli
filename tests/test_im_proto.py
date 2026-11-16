"""Frozen, synthetic protobuf contracts; no generated schema, device or network."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosscli import im_proto as proto
from bosscli.im_mqtt import IncomingPublish, Packet
from bosscli.im_proto import (ChatProtocolError, ImageInfo, blob, business_ack,
    encode_image_message, fields, number, text, varint)


# Independently frozen wire bytes, using tiny synthetic URLs and IDs 1/2/3/4.
GOLDEN = bytes.fromhex(
    "08011203312e341a5f"
    "0a07080112014e3801"  # from(field 1): uid=1/name=N/source=1
    "120408023802"        # to(field 2): uid=2/source=2
    "180120032804"        # type=1, mid(field 4)=3, time(field 5)=4
    "3238"                # body(field 6), 56 bytes
    "080310012a32"        # body type=3/template=1/image(field 5)
    "0800"                # image type=0
    "12160a1068747470733a2f2f742e746573742f7410011802"  # tiny(field 2)
    "1a160a1068747470733a2f2f6f2e746573742f6f100a1814"  # origin(field 3)
    "38005000580360026800900100a00100")


def encode(**updates):
    args = dict(from_uid=1, to_uid=2, client_mid=3,
                origin=ImageInfo("https://o.test/o", 10, 20),
                tiny=ImageInfo("https://t.test/t", 1, 2),
                from_source=1, to_source=2, sender_name="N", now_ms=4)
    return encode_image_message(**{**args, **updates})


def one(parsed, tag, wire):
    assert len(parsed[tag]) == 1
    assert parsed[tag][0][0] == wire
    return parsed[tag][0][1]


def ack(*pairs, message_type=5):
    return number(1, message_type) + b"".join(blob(7, number(1, client) + number(2, server))
                                                for client, server in pairs)


def test_image_message_matches_complete_independent_frozen_wire():
    assert encode() == GOLDEN
    assert encode() == encode()  # no nondeterministic IDs/times in a frozen call


def test_schema_numbers_exact_nested_fields_and_explicit_optional_defaults():
    root = fields(GOLDEN)
    assert set(root) == {1, 2, 3}
    assert one(root, 1, 0) == 1
    assert one(root, 2, 2) == b"1.4"
    message = fields(one(root, 3, 2))
    assert set(message) == {1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 18, 20}
    assert fields(one(message, 1, 2)) == {1: [(0, 1)], 2: [(2, b"N")], 7: [(0, 1)]}
    assert fields(one(message, 2, 2)) == {1: [(0, 2)], 7: [(0, 2)]}
    assert one(message, 3, 0) == 1
    assert one(message, 4, 0) == one(message, 11, 0) == 3
    assert one(message, 5, 0) == 4
    assert {tag: one(message, tag, 0) for tag in (7, 10, 12, 13, 18, 20)} == {
        7: 0, 10: 0, 12: 2, 13: 0, 18: 0, 20: 0}
    body = fields(one(message, 6, 2))
    assert set(body) == {1, 2, 5}
    assert one(body, 1, 0) == 3 and one(body, 2, 0) == 1
    image = fields(one(body, 5, 2))
    assert set(image) == {1, 2, 3} and one(image, 1, 0) == 0
    assert fields(one(image, 2, 2)) == {1: [(2, b"https://t.test/t")], 2: [(0, 1)], 3: [(0, 2)]}
    assert fields(one(image, 3, 2)) == {1: [(2, b"https://o.test/o")], 2: [(0, 10)], 3: [(0, 20)]}


def test_implicit_clock_is_freezable_and_empty_sender_name_is_omitted(monkeypatch):
    monkeypatch.setattr(proto.time, "time", lambda: 1.234)
    root = fields(encode(now_ms=None, sender_name="", from_source=0, to_source=0))
    message = fields(one(root, 3, 2))
    assert one(message, 5, 0) == 1234
    assert fields(one(message, 1, 2)) == {1: [(0, 1)], 7: [(0, 0)]}
    assert fields(one(message, 2, 2)) == {1: [(0, 2)], 7: [(0, 0)]}


def test_image_descriptor_is_frozen_and_repr_str_hide_authorized_url():
    url = "https://img.bosszhipin.com/synthetic-private?token=synthetic-secret"
    image = ImageInfo(url, 1, 2)
    assert image.encode() == text(1, url) + b"\x10\x01\x18\x02"
    assert url not in repr(image) and "synthetic-secret" not in str(image)
    with pytest.raises(FrozenInstanceError):
        image.width = 9


def test_packet_and_incoming_payload_repr_hide_entire_private_protobuf():
    url = "https://img.bosszhipin.com/synthetic-private?token=synthetic-secret"
    payload = encode(origin=ImageInfo(url, 10, 20))
    for obj in (Packet(0x30, payload), IncomingPublish("chat", payload)):
        assert repr(payload) not in repr(obj)
        assert "synthetic-private" not in repr(obj) and "synthetic-secret" not in str(obj)


@pytest.mark.parametrize("url", [None, 3, "", "http://o.test/x", "file:///x", "https:///x",
    "https://user:secret@o.test/x", "https://user@o.test/x", "https://[broken/x",
    "https://o.test/x\x00secret", "https://o.test/x\nsecret"])
def test_invalid_image_urls_fail_without_echoing_input(url):
    with pytest.raises(ChatProtocolError) as exc:
        ImageInfo(url, 1, 2)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("dimension", [0, -1, 100001, True, False, 1.2, "1", None])
def test_image_dimensions_are_positive_bounded_integers(dimension):
    for width, height in ((dimension, 1), (1, dimension)):
        with pytest.raises(ChatProtocolError):
            ImageInfo("https://o.test/x", width, height)


@pytest.mark.parametrize("updates", [{"from_uid": 0}, {"from_uid": True}, {"from_uid": "1"},
    {"to_uid": 0}, {"to_uid": 1}, {"to_uid": 1 << 63}, {"client_mid": 0},
    {"client_mid": True}, {"client_mid": 1 << 63}, {"now_ms": 0}, {"now_ms": True},
    {"now_ms": 1.2}, {"from_source": -1}, {"from_source": 65536}, {"to_source": True},
    {"to_source": 65536}, {"origin": {}}, {"tiny": None}, {"sender_name": 3}])
def test_image_encoder_rejects_invalid_context_before_building_payload(updates):
    with pytest.raises(ChatProtocolError):
        encode(**updates)


def test_long_limits_and_full_source_range_are_encoded_without_float_loss():
    raw = encode(from_uid=(1 << 63) - 1, to_uid=(1 << 63) - 2,
                 client_mid=(1 << 63) - 3, now_ms=(1 << 63) - 4,
                 from_source=65535, to_source=65535)
    message = fields(one(fields(raw), 3, 2))
    assert one(message, 4, 0) == one(message, 11, 0) == (1 << 63) - 3
    assert one(message, 5, 0) == (1 << 63) - 4
    assert one(fields(one(message, 1, 2)), 7, 0) == 65535


@pytest.mark.parametrize("value,golden", [(0, "00"), (1, "01"), (127, "7f"), (128, "8001"),
    (16384, "808001"), ((1 << 64) - 1, "ffffffffffffffffff01")])
def test_varints_frozen_boundaries(value, golden):
    assert varint(value) == bytes.fromhex(golden)
    assert fields(b"\x08" + bytes.fromhex(golden)) == {1: [(0, value)]}


def test_signed_java_integer_encoding_and_fixed_width_unknown_fields():
    assert number(1, -1) == b"\x08" + b"\xff" * 9 + b"\x01"
    assert fields(b"\x09" + b"a" * 8 + b"\x15" + b"b" * 4) == {
        1: [(1, b"a" * 8)], 2: [(5, b"b" * 4)]}


@pytest.mark.parametrize("bad", [-1, True, 1.2, "1", None, 1 << 64])
def test_varint_rejects_invalid_values(bad):
    with pytest.raises(ChatProtocolError):
        varint(bad)


@pytest.mark.parametrize("tag", [0, -1, True, 1 << 29, "1"])
def test_field_builders_reject_invalid_field_numbers(tag):
    for builder, value in ((number, 1), (blob, b"x"), (text, "x")):
        with pytest.raises(ChatProtocolError):
            builder(tag, value)


@pytest.mark.parametrize("raw", [b"\x00", b"\x01" + b"x" * 8, b"\x0b", b"\x0c", b"\x0e", b"\x0f",
    b"\x80", b"\x08", b"\x08\x80", b"\x08" + b"\x80" * 10,
    b"\x08" + b"\xff" * 9 + b"\x02", b"\x12", b"\x12\x80", b"\x12\x03ab",
    b"\x09short", b"\x15abc", b"\x80\x80\x80\x80\x10\x00"])
def test_fields_reject_truncation_overflow_invalid_tag_and_unsupported_wire(raw):
    with pytest.raises(ChatProtocolError):
        fields(raw)


def test_field_decoder_and_builders_have_explicit_size_and_count_caps(monkeypatch):
    monkeypatch.setattr(proto, "MAX_PROTO_BYTES", 8)
    for raw in ("not bytes", bytearray(b"x"), b"x" * 9):
        with pytest.raises(ChatProtocolError):
            fields(raw)
    with pytest.raises(ChatProtocolError):
        blob(1, b"x" * 9)
    with pytest.raises(ChatProtocolError):
        blob(1, bytearray(b"x"))
    monkeypatch.setattr(proto, "MAX_FIELDS", 2)
    assert fields(b"\x08\x01\x08\x02") == {1: [(0, 1), (0, 2)]}
    with pytest.raises(ChatProtocolError, match="count cap"):
        fields(b"\x08\x01" * 3)


def test_business_ack_frozen_type5_sync7_and_exact_client_match():
    golden = bytes.fromhex("08053a0408031009")
    assert ack((3, 9)) == golden
    assert business_ack(golden, 3) == 9
    assert business_ack(golden, 2) is None
    assert business_ack(ack((2, 8), (3, 9), (4, 10)), 3) == 9
    assert business_ack(number(1, 5), 3) is None
    assert business_ack(GOLDEN, 3) is None  # image echo is not an ACK


@pytest.mark.parametrize("message_type", [0, 1, 3, 4, 6, 7])
def test_other_root_types_never_confirm_even_with_matching_ids(message_type):
    assert business_ack(ack((3, 9), message_type=message_type), 3) is None


@pytest.mark.parametrize("payload", [
    b"", blob(1, b"5"), number(1, 5) * 2,
    number(1, 5) + number(7, 3),
    number(1, 5) + blob(7, number(1, 3)),
    number(1, 5) + blob(7, number(2, 9)),
    number(1, 5) + blob(7, text(1, "3") + number(2, 9)),
    number(1, 5) + blob(7, number(1, 3) * 2 + number(2, 9)),
    number(1, 5) + blob(7, number(1, 3) + number(2, 9) * 2),
    ack((3, 9), (3, 9)), ack((3, 9), (3, 10)),
    ack((0, 9)), ack((3, 0)), ack((-1, 9)), ack((3, -1)),
    b"\x08\x05\x3a\x04\x08\x03\x10", b"\x08\x05\x3a\x01\x0b"])
def test_ack_truncation_wrong_wire_missing_duplicate_or_invalid_ids_rejected(payload):
    with pytest.raises(ChatProtocolError):
        business_ack(payload, 3)


@pytest.mark.parametrize("expected", [0, -1, True, "3", None, 1 << 63])
def test_ack_expected_id_must_be_positive_long(expected):
    with pytest.raises(ChatProtocolError):
        business_ack(ack((3, 9)), expected)


def test_ack_invalid_unrelated_sync_is_not_ignored_and_errors_hide_payload():
    payload = ack((3, 9), (4, 0)) + text(99, "synthetic-authorized-token")
    with pytest.raises(ChatProtocolError) as exc:
        business_ack(payload, 3)
    assert "synthetic-authorized-token" not in str(exc.value)
