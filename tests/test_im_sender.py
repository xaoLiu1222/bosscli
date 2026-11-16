"""Synthetic sender integration: fake TLS/connection/HTTP, never a remote socket."""
from collections import deque
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import socket
import ssl
import struct
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosscli.contacts import SelectedJob, parse_relation
from bosscli import im_sender as sender_module
from bosscli.im_mqtt import (MQTTError, MQTTProtocolError, MQTTTimeout, Packet, PubAck,
    TransportAckOnly, decode_puback, decode_packet, decode_publish, encode_publish)
from bosscli.im_proto import ChatProtocolError, blob, fields, number
from bosscli.im_sender import ImageSender


PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082")
SECRET = "synthetic-im-secret-not-real"
MID = 1700000000001
SERVER_MID = 1800000000002


class FakeTLSContext:
    def __init__(self):
        self.minimum_version = None
        self.cert_calls = []

    def load_cert_chain(self, cert_file, key_file):
        self.cert_calls.append((cert_file, key_file))


class FakeConnection:
    """Model only the sender boundary, reusing the existing PUBACK decoder contract."""
    def __init__(self):
        self.events, self.auths, self.published, self.received_timeouts = [], [], [], []
        self.connect_error = self.subscribe_error = self.publish_error = None
        self.disconnect_error = None
        self.puback = Packet(0x40, struct.pack(">Hqq", 7, MID, SERVER_MID))
        self.incoming = deque()
        self.default_packet = None

    def connect(self, auth):
        self.events.append("connect")
        self.auths.append(auth)
        if self.connect_error:
            raise self.connect_error

    def subscribe_chat(self):
        self.events.append("subscribe")
        if self.subscribe_error:
            raise self.subscribe_error
        return None  # verified transport permits packet-id-only SUBACK; do not guess QoS

    def publish(self, payload, expected_client_mid, *, timeout):
        self.events.append("publish")
        self.published.append((payload, expected_client_mid, timeout))
        if self.publish_error:
            raise self.publish_error
        ack = decode_puback(self.puback, expected_sequence=7, expected_client_mid=expected_client_mid)
        if ack.transport_only:
            raise TransportAckOnly(ack)
        return ack.server_mid

    def receive(self, *, timeout):
        self.received_timeouts.append(timeout)
        if self.incoming:
            value = self.incoming.popleft()
            if isinstance(value, Exception):
                raise value
            return value
        if self.default_packet is not None:
            return self.default_packet
        raise MQTTTimeout("synthetic receive timeout; delivery unknown")

    def disconnect(self, *, timeout):
        assert timeout == 2
        self.events.append("disconnect")
        if self.disconnect_error:
            raise self.disconnect_error

    def close(self):
        self.events.append("close")


class FakeFactory:
    def __init__(self, connection):
        self.connection, self.calls = connection, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.connection


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("synthetic sender tests must never use HTTP or a real socket")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def tls(monkeypatch):
    contexts, calls = [], []
    def create_context(*, cafile):
        calls.append(cafile)
        context = FakeTLSContext()
        contexts.append(context)
        return context
    monkeypatch.setattr(sender_module.ssl, "create_default_context", create_context)
    return contexts, calls


@pytest.fixture
def session(tmp_path):
    paths = {}
    for name in ("ca_file", "cert_file", "key_file"):
        path = tmp_path / (name + ".synthetic")
        path.write_text("synthetic test placeholder; NOT a certificate/key", encoding="utf-8")
        paths[name] = str(path)
    return {"uid": 300003, "curidentity": 0, "secretKey": SECRET,
            "name": "Synthetic Account", "v": "14.180", "uniqid": "synthetic-device-id",
            "im": {**paths, "timeout": 2}}


@pytest.fixture
def context():
    job = SelectedJob("Synthetic Engineer", "Synthetic Co", 100001, 200002,
                      "synthetic-search-token", "synthetic-lid", "Synthetic Recruiter")
    return parse_relation(job, {"friendId": 200002, "jobId": 100001,
        "friendSource": 2, "userSource": 1, "jobSource": 7,
        "securityId": "synthetic-relation-chat-token"})


@pytest.fixture
def upload():
    return {"code": 0, "zpData": {
        "url": "https://imgaz.bosszhipin.com/synthetic-origin?token=private",
        "tinyUrl": "https://img.bosszhipin.com/synthetic-tiny?token=private",
        "metadata": {"width": 40, "height": 30}, "tinyWidth": 4, "tinyHeight": 3},
        "_upload": {"width": 400, "height": 300}}


@pytest.fixture
def harness(session, tls, monkeypatch):
    connection = FakeConnection()
    factory = FakeFactory(connection)
    thumbnails = []
    def thumbnail(url):
        thumbnails.append(url)
        return 4, 3
    monkeypatch.setattr(ImageSender, "_thumbnail_size", staticmethod(thumbnail))
    sender = ImageSender(session, connection_factory=factory)
    return sender, connection, factory, thumbnails


def single(parsed, tag, wire):
    assert len(parsed[tag]) == 1 and parsed[tag][0][0] == wire
    return parsed[tag][0][1]


def message_parts(payload):
    root = fields(payload)
    assert single(root, 1, 0) == 1 and single(root, 2, 2) == b"1.4"
    message = fields(single(root, 3, 2))
    body = fields(single(message, 6, 2))
    image = fields(single(body, 5, 2))
    return message, fields(single(image, 2, 2)), fields(single(image, 3, 2))


def incoming_ack(client_mid=MID, server_mid=SERVER_MID, *, topic="chat", message_type=5):
    payload = number(1, message_type) + blob(7, number(1, client_mid) + number(2, server_mid))
    return decode_packet(encode_publish(payload, 9, topic=topic))


def transport_only(connection):
    connection.puback = Packet(0x40, b"\x00\x07")


def test_type5_already_buffered_confirms_after_puback_timeout(harness, context, upload):
    sender, connection, _, _ = harness
    sender.open()
    connection.publish_error = MQTTTimeout("synthetic lost PUBACK")
    pending = deque([incoming_ack()])
    connection.pop_pending = lambda: pending.popleft() if pending else None
    assert sender.send_image(context, upload, client_mid=MID) == SERVER_MID
    assert len(connection.published) == 1 and connection.received_timeouts == []


def test_timeout_without_buffered_matching_ack_remains_unknown(harness, context, upload):
    sender, connection, _, _ = harness
    sender.open()
    connection.publish_error = MQTTTimeout("synthetic lost PUBACK")
    pending = deque([incoming_ack(client_mid=MID + 1)])
    connection.pop_pending = lambda: pending.popleft() if pending else None
    with pytest.raises(MQTTTimeout):
        sender.send_image(context, upload, client_mid=MID)
    assert len(connection.published) == 1 and connection.received_timeouts == []


def test_constructor_uses_fake_tls_only_and_does_not_open_socket(session, tls):
    connection, calls = FakeConnection(), []
    def factory(**kwargs):
        calls.append(kwargs)
        return connection
    sender = ImageSender(session, connection_factory=factory)
    contexts, ca_calls = tls
    assert calls == [] and sender.connection is None
    assert ca_calls == [session["im"]["ca_file"]]
    assert contexts[0].minimum_version == ssl.TLSVersion.TLSv1_2
    assert contexts[0].cert_calls == [(session["im"]["cert_file"], session["im"]["key_file"])]


@pytest.mark.parametrize("host", ["chat.zhipin.com", "mqtt-ecc.zhipin.com"])
def test_open_injects_exact_tls_host_443_timeout_and_sanitized_auth(session, tls, monkeypatch, host):
    session["im"]["host"] = host
    session["im"]["device_id"] = "synthetic-explicit-device"
    monkeypatch.setattr(sender_module.time, "time", lambda: 1700000000.0)
    connection = FakeConnection()
    factory = FakeFactory(connection)
    sender = ImageSender(session, connection_factory=factory)
    sender.open()
    assert connection.events == ["connect", "subscribe"]
    assert sender.connection is connection
    assert len(factory.calls) == 1
    args = factory.calls[0]
    assert args == {"socket_factory": socket.create_connection, "host": host, "port": 443,
                    "ssl_context": tls[0][0], "timeout": 2}
    auth = connection.auths[0]
    assert auth.username == "300003-0-1.4-14.180"
    assert SECRET not in repr(auth) and auth.password not in repr(auth)
    with pytest.raises(MQTTError, match="already open"):
        sender.open()
    assert len(factory.calls) == 1
    sender.close()
    sender.close()
    assert connection.events == ["connect", "subscribe", "disconnect"]
    assert sender.connection is None


@pytest.mark.parametrize("where", ["connect", "subscribe"])
def test_login_failure_closes_without_publish_or_automatic_reconnect(harness, where):
    sender, connection, factory, _ = harness
    setattr(connection, where + "_error", MQTTError("synthetic login failure"))
    with pytest.raises(MQTTError, match="login failure"):
        sender.open()
    expected = ["connect", "close"] if where == "connect" else ["connect", "subscribe", "close"]
    assert connection.events == expected and sender.connection is None
    assert connection.published == [] and len(factory.calls) == 1


def test_factory_failure_is_not_retried(session, tls):
    calls = []
    def factory(**kwargs):
        calls.append(kwargs)
        raise OSError("synthetic connection creation failed")
    sender = ImageSender(session, connection_factory=factory)
    with pytest.raises(OSError):
        sender.open()
    assert len(calls) == 1 and sender.connection is None


def test_matching_extended_puback_confirms_one_publish_with_bound_source_fields(harness, context, upload, monkeypatch):
    sender, connection, _, thumbnails = harness
    monkeypatch.setattr(sender_module.time, "time", lambda: 1700000000.0)
    sender.open()
    before = deepcopy(upload)
    assert sender.send_image(context, upload, client_mid=MID) == SERVER_MID
    assert len(connection.published) == 1 and connection.received_timeouts == [] and thumbnails == []
    payload, expected_mid, timeout = connection.published[0]
    assert (expected_mid, timeout) == (MID, 2)
    message, tiny, origin = message_parts(payload)
    assert fields(single(message, 1, 2)) == {1: [(0, 300003)],
        2: [(2, b"Synthetic Account")], 7: [(0, context.user_source)]}
    assert fields(single(message, 2, 2)) == {1: [(0, context.friend_id)], 7: [(0, 2)]}
    assert single(message, 4, 0) == single(message, 11, 0) == MID
    assert single(message, 5, 0) == 1700000000000
    assert (single(tiny, 2, 0), single(tiny, 3, 0)) == (4, 3)
    assert (single(origin, 2, 0), single(origin, 3, 0)) == (40, 30)
    assert single(origin, 1, 2) == upload["zpData"]["url"].encode()
    assert context.security_id.encode() not in payload
    assert upload == before
    sender.close()


def test_missing_thumbnail_dimensions_uses_injected_size_and_local_origin_fallback(harness, context, upload):
    sender, connection, _, thumbnails = harness
    del upload["zpData"]["tinyWidth"]
    del upload["zpData"]["tinyHeight"]
    del upload["zpData"]["metadata"]
    sender.open()
    assert sender.send_image(context, upload, client_mid=MID) == SERVER_MID
    assert thumbnails == [upload["zpData"]["tinyUrl"]]
    _, tiny, origin = message_parts(connection.published[0][0])
    assert (single(tiny, 2, 0), single(tiny, 3, 0)) == (4, 3)
    assert (single(origin, 2, 0), single(origin, 3, 0)) == (400, 300)


@pytest.mark.parametrize("bad_context", [None, {}, 200002, "synthetic-person"])
def test_send_requires_real_bound_context_and_open_connection(harness, context, upload, bad_context):
    sender, connection, _, _ = harness
    with pytest.raises(ValueError):
        sender.send_image(context, upload, client_mid=MID)
    sender.open()
    with pytest.raises(ValueError):
        sender.send_image(bad_context, upload, client_mid=MID)
    assert connection.published == []


@pytest.mark.parametrize("mid", [0, -1, True, "1", None, 1 << 63])
def test_invalid_client_mid_fails_before_publish(harness, context, upload, mid):
    sender, connection, _, _ = harness
    sender.open()
    with pytest.raises(ChatProtocolError):
        sender.send_image(context, upload, client_mid=mid)
    assert connection.published == []


@pytest.mark.parametrize("updates", [{"url": "http://unsafe.invalid/x"}, {"url": None},
    {"tinyUrl": "http://unsafe.invalid/x"}, {"metadata": {"width": 0, "height": 30}},
    {"metadata": {"width": True, "height": 30}}, {"tinyWidth": -1}])
def test_invalid_image_descriptors_fail_before_publish(harness, context, upload, updates):
    sender, connection, _, _ = harness
    upload["zpData"].update(updates)
    sender.open()
    with pytest.raises((ChatProtocolError, ValueError)):
        sender.send_image(context, upload, client_mid=MID)
    assert connection.published == []


def test_matching_type5_after_transport_only_puback_is_business_success(harness, context, upload):
    sender, connection, _, _ = harness
    transport_only(connection)
    connection.incoming.extend([Packet(0xD0, b""), incoming_ack(MID + 1),
        incoming_ack(topic="unrelated"), incoming_ack(message_type=1), incoming_ack()])
    sender.open()
    assert sender.send_image(context, upload, client_mid=MID) == SERVER_MID
    assert len(connection.published) == 1 and len(connection.received_timeouts) == 5
    assert all(0 < timeout <= 2 for timeout in connection.received_timeouts)


def test_wrong_client_id_extended_puback_is_never_blind_success(harness, context, upload):
    sender, connection, factory, _ = harness
    connection.puback = Packet(0x40, struct.pack(">Hqq", 7, MID + 1, SERVER_MID))
    sender.open()
    with pytest.raises(MQTTProtocolError, match="client message ID mismatch"):
        sender.send_image(context, upload, client_mid=MID)
    assert len(connection.published) == 1 and len(factory.calls) == 1
    assert connection.received_timeouts == []


def test_only_wrong_id_business_ack_then_timeout_is_unknown_not_success(harness, context, upload):
    sender, connection, factory, _ = harness
    transport_only(connection)
    connection.incoming.append(incoming_ack(MID + 1))
    sender.open()
    with pytest.raises(MQTTTimeout):
        sender.send_image(context, upload, client_mid=MID)
    assert len(connection.published) == 1 and len(factory.calls) == 1
    assert len(connection.received_timeouts) == 2


@pytest.mark.parametrize("error", [MQTTTimeout("synthetic publish timeout"), MQTTError("synthetic failure"),
                                 OSError("synthetic socket failure")])
def test_publish_failure_propagates_without_retry_or_ack_fallback(harness, context, upload, error):
    sender, connection, factory, _ = harness
    connection.publish_error = error
    sender.open()
    with pytest.raises(type(error)):
        sender.send_image(context, upload, client_mid=MID)
    assert len(connection.published) == 1 and connection.received_timeouts == [] and len(factory.calls) == 1


def test_business_ack_wait_has_128_packet_bound_without_wall_clock_wait(harness, context, upload, monkeypatch):
    sender, connection, _, _ = harness
    transport_only(connection)
    connection.default_packet = incoming_ack(MID + 1)
    monkeypatch.setattr(sender_module.time, "monotonic", lambda: 100.0)
    sender.open()
    with pytest.raises(MQTTError, match="接收数据过多"):
        sender.send_image(context, upload, client_mid=MID)
    assert len(connection.received_timeouts) == 128 and len(connection.published) == 1


def test_business_ack_wait_has_total_deadline_not_timeout_per_packet(harness, context, upload, monkeypatch):
    sender, connection, _, _ = harness
    transport_only(connection)
    connection.incoming.append(incoming_ack(MID + 1))
    clock = iter([100.0, 100.5, 102.0])
    monkeypatch.setattr(sender_module.time, "monotonic", lambda: next(clock))
    sender.open()
    with pytest.raises(MQTTTimeout, match="请勿盲目重发"):
        sender.send_image(context, upload, client_mid=MID)
    assert connection.received_timeouts == [1.5] and len(connection.published) == 1


def test_duplicate_matching_type5_sync_is_rejected_not_guessed(harness, context, upload):
    sender, connection, _, _ = harness
    transport_only(connection)
    sync = blob(7, number(1, MID) + number(2, SERVER_MID))
    connection.incoming.append(decode_packet(encode_publish(number(1, 5) + sync + sync, 9)))
    sender.open()
    with pytest.raises(ChatProtocolError, match="duplicate"):
        sender.send_image(context, upload, client_mid=MID)
    assert len(connection.published) == 1


def test_new_mid_is_monotonic_even_if_clock_stalls_or_moves_backwards(harness, monkeypatch):
    sender, _, _, _ = harness
    times = iter([100.0, 100.0, 99.0, 101.0])
    monkeypatch.setattr(sender_module.time, "time", lambda: next(times))
    assert [sender.new_client_mid() for _ in range(4)] == [100000, 100001, 100002, 101000]


def test_context_manager_closes_on_body_error_and_disconnect_failure_falls_back(harness):
    sender, connection, _, _ = harness
    connection.disconnect_error = MQTTError("synthetic disconnect failure")
    with pytest.raises(RuntimeError, match="synthetic body"):
        with sender as opened:
            assert opened is sender
            raise RuntimeError("synthetic body")
    assert sender.connection is None
    assert connection.events == ["connect", "subscribe", "disconnect", "close"]


@pytest.mark.parametrize("im", [None, [], "config", 3])
def test_missing_or_malformed_im_config_fails_before_tls_or_factory(session, tls, im):
    session["im"] = im
    factory = FakeFactory(FakeConnection())
    with pytest.raises(ValueError):
        ImageSender(session, connection_factory=factory)
    assert factory.calls == [] and tls[1] == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "169.254.169.254", "evil.invalid",
    "chat.zhipin.com.evil.invalid", "https://chat.zhipin.com", "CHAT.ZHIPIN.COM", None, ""])
def test_tls_host_is_exact_allowlist_before_tls_creation(session, tls, host):
    session["im"]["host"] = host
    with pytest.raises(ValueError):
        ImageSender(session, connection_factory=FakeFactory(FakeConnection()))
    assert tls[1] == []


@pytest.mark.parametrize("port", [80, 8883, 0, -1, "443", None, True, 443.0])
def test_tls_port_must_be_integer_443_before_tls_creation(session, tls, port):
    session["im"]["port"] = port
    with pytest.raises(ValueError):
        ImageSender(session, connection_factory=FakeFactory(FakeConnection()))
    assert tls[1] == []


@pytest.mark.parametrize("timeout", [None, "2", True, False, 0, -1, .5, 60.1, float("nan"), float("inf")])
def test_timeout_must_be_finite_and_bounded_before_tls_creation(session, tls, timeout):
    session["im"]["timeout"] = timeout
    with pytest.raises(ValueError):
        ImageSender(session, connection_factory=FakeFactory(FakeConnection()))
    assert tls[1] == []


@pytest.mark.parametrize("name", ["ca_file", "cert_file", "key_file"])
@pytest.mark.parametrize("bad", [None, "", "missing-synthetic-file", "directory", "absent"])
def test_local_tls_material_must_be_explicit_existing_files_before_context(session, tls, tmp_path, name, bad):
    if bad == "absent":
        del session["im"][name]
    else:
        session["im"][name] = str(tmp_path) if bad == "directory" else bad
    with pytest.raises(ValueError):
        ImageSender(session, connection_factory=FakeFactory(FakeConnection()))
    assert tls[1] == []


@pytest.mark.parametrize("name,bad", [("uid", None), ("uid", 0), ("uid", -1), ("uid", True),
    ("uid", 1.9), ("uid", "not-numeric"), ("uid", "1.0"), ("uid", 1 << 63),
    ("curidentity", True), ("curidentity", False), ("curidentity", .5), ("curidentity", 1),
    ("curidentity", "x"), ("secretKey", ""), ("secretKey", None), ("secretKey", 12),
    ("secretKey", []), ("v", ""), ("v", None), ("v", 12), ("v", "bad\x00version"),
    ("uniqid", 12)])
def test_malformed_account_auth_config_fails_at_constructor_before_tls(session, tls, name, bad):
    session[name] = bad
    factory = FakeFactory(FakeConnection())
    with pytest.raises(ValueError):
        ImageSender(session, connection_factory=factory)
    assert tls[1] == [] and factory.calls == []


def test_bad_explicit_device_id_fails_constructor_not_after_greeting(session, tls):
    session["im"]["device_id"] = 12
    with pytest.raises(ValueError):
        ImageSender(session, connection_factory=FakeFactory(FakeConnection()))
    assert tls[1] == []


def test_invalid_certificate_chain_stops_before_factory(session, monkeypatch):
    class BrokenTLS(FakeTLSContext):
        def load_cert_chain(self, *args):
            raise ssl.SSLError("synthetic invalid local certificate")
    monkeypatch.setattr(sender_module.ssl, "create_default_context", lambda **kwargs: BrokenTLS())
    factory = FakeFactory(FakeConnection())
    with pytest.raises(ssl.SSLError):
        ImageSender(session, connection_factory=factory)
    assert factory.calls == []


class FakeResponse:
    def __init__(self, *, status=200, chunks=(PNG_BYTES,), error=None):
        self.status_code, self.chunks, self.error = status, chunks, error
        self.closed, self.chunk_sizes = False, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def iter_content(self, chunk_size):
        self.chunk_sizes.append(chunk_size)
        if self.error:
            raise self.error
        yield from self.chunks


def install_http(monkeypatch, response):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return response
    monkeypatch.setattr(requests, "get", get)
    return calls


@pytest.mark.parametrize("host", ["imgaz.bosszhipin.com", "img.bosszhipin.com", "img2.bosszhipin.com"])
@pytest.mark.parametrize("port", ["", ":443"])
def test_thumbnail_only_fetches_allowed_https_cdn_without_redirects(monkeypatch, host, port):
    response = FakeResponse(chunks=(PNG_BYTES[:20], b"", PNG_BYTES[20:]))
    calls = install_http(monkeypatch, response)
    url = f"https://{host}{port}/synthetic.png?token=synthetic"
    assert ImageSender._thumbnail_size(url) == (1, 1)
    assert calls == [(url, {"timeout": 10, "allow_redirects": False, "stream": True})]
    assert response.closed and response.chunk_sizes == [32768]


@pytest.mark.parametrize("url", [None, 3, "", "http://img.bosszhipin.com/x", "file:///etc/passwd",
    "https://127.0.0.1/x", "https://localhost/x", "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/x", "https://evil.invalid/x", "https://img.bosszhipin.com.evil.invalid/x",
    "https://img.bosszhipin.com@127.0.0.1/x", "https://user:secret@img.bosszhipin.com/x",
    "https://img.bosszhipin.com:80/x", "https://img.bosszhipin.com:444/x",
    "https://img.bosszhipin.com:not-a-port/x", "https://[broken/x", "//img.bosszhipin.com/x"])
def test_thumbnail_ssrf_targets_rejected_before_any_http(monkeypatch, url):
    calls = install_http(monkeypatch, FakeResponse())
    with pytest.raises(ValueError):
        ImageSender._thumbnail_size(url)
    assert calls == []


@pytest.mark.parametrize("status", [301, 302, 307, 403, 404, 500])
def test_thumbnail_redirect_or_failure_is_not_followed(monkeypatch, status):
    response = FakeResponse(status=status)
    calls = install_http(monkeypatch, response)
    with pytest.raises(ValueError, match="无法读取"):
        ImageSender._thumbnail_size("https://img.bosszhipin.com/synthetic.png")
    assert len(calls) == 1 and response.closed and response.chunk_sizes == []


def test_thumbnail_stream_byte_limit_is_enforced_before_image_decode(monkeypatch):
    response = FakeResponse(chunks=(b"x" * (2 * 1024 * 1024), b"x"))
    install_http(monkeypatch, response)
    with pytest.raises(ValueError, match="大小限制"):
        ImageSender._thumbnail_size("https://img.bosszhipin.com/synthetic.png")
    assert response.closed


def test_thumbnail_pixel_limit_is_enforced_without_allocating_huge_image(monkeypatch):
    from PIL import Image
    class HugeImage:
        width, height = 2001, 2000
        size = (width, height)
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return None
    monkeypatch.setattr(Image, "open", lambda *_: HugeImage())
    response = FakeResponse()
    install_http(monkeypatch, response)
    with pytest.raises(ValueError, match="像素限制"):
        ImageSender._thumbnail_size("https://img.bosszhipin.com/synthetic.png")
    assert response.closed


def test_thumbnail_request_exception_is_safe_and_does_not_retry(monkeypatch):
    response = FakeResponse(error=requests.ConnectionError("synthetic-private-token"))
    calls = install_http(monkeypatch, response)
    with pytest.raises(ValueError, match="不要重复发送") as exc:
        ImageSender._thumbnail_size("https://img.bosszhipin.com/synthetic.png?token=synthetic-private-token")
    assert len(calls) == 1 and response.closed
    assert "synthetic-private-token" not in str(exc.value)
