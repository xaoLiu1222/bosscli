"""Upload contract tests. Synthetic credentials/images only; no remote requests.

The localhost HTTP test verifies requests' real multipart serialization and gzip decode,
not merely an internally self-consistent dict. Historical capture comparison is separate.
"""
import gzip
import io
import json
import sys
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosscli import signer, yzwg
from bosscli.client import (AuthExpired, BossClient, UPLOAD_HOST, UPLOAD_IMAGE_PATH,
                            UPLOAD_PRECHECK_PATH)
from bosscli.upload import (UploadError, MAX_IMAGE_BYTES, PreparedImage, prepare_image,
                           upload_envelope)

SECRET = "synthetic-upload-key-not-a-real-account"
CONTEXT = "synthetic-upload-context"
SESSION = {"t2": "synthetic-t2", "secretKey": SECRET, "v": "14.180", "app_id": "1003",
           "uniqid": "synthetic-device", "curidentity": "0",
           "client_info": {"os": "Android", "uniqid": "synthetic-device"},
           "upload_defaults": {"securityId": CONTEXT}}
SUCCESS_DATA = {"url": "https://example.invalid/image.jpg", "tinyUrl": "https://example.invalid/tiny.jpg",
                "metadata": {"width": 8, "height": 12, "fileSize": 100}}


def response(data=None, code=0, status=200):
    return SimpleNamespace(status_code=status, content=json.dumps({"code": code, "message": "test",
                           "zpData": {} if data is None else data}).encode())


@pytest.fixture
def picture(tmp_path):
    path = tmp_path / "图.png"
    Image.new("RGBA", (8, 12), (80, 120, 160, 255)).save(path)
    return path


def capture(responses, session=None):
    c = BossClient(dict(SESSION) if session is None else session)
    calls = []
    replies = iter(responses)
    def fake_http(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return next(replies)
    c._http = fake_http
    return c, calls


def decode_call(call):
    if call.get("files"):
        parts = call["files"]
        assert [p[0] for p in parts] == ["sp", "sig", "app_id", "file"]
        fields = {k: v[1] for k, v in parts[:3]}
    else:
        fields = call["data"]
    assert set(fields) == {"sp", "sig", "app_id"}
    assert fields["app_id"] == "1003"
    path = urlsplit(call["url"]).path
    assert not urlsplit(call["url"]).query
    plain = yzwg.native_decode_content(fields["sp"], SECRET).decode()
    assert fields["sig"] == yzwg.native_signature((path + plain[:5000]).encode(), SECRET)
    assert fields["sig"] != yzwg.native_signature((path + plain[:5000]).encode(), None)
    params = dict(parse_qsl(plain, keep_blank_values=True))
    assert params["securityId"] == CONTEXT and params["source"] == "chat_file"
    assert "app_id" not in params
    assert call["retries"] == 0 and call["allow_redirects"] is False
    return params


def test_precheck_miss_then_upload_matches_wire_contract(picture):
    c, calls = capture([response(), response(SUCCESS_DATA)])
    result = c.upload_image(str(picture))
    assert len(calls) == 2
    quick, single = calls
    assert quick["url"] == UPLOAD_HOST + UPLOAD_PRECHECK_PATH
    assert single["url"] == UPLOAD_HOST + UPLOAD_IMAGE_PATH
    assert quick["method"] == single["method"] == "POST"
    q = decode_call(quick)
    p = decode_call(single)
    assert q["gid"] == "" and set(q) == {"client_info", "curidentity", "req_time", "uniqid", "v",
                                            "source", "securityId", "gid", "fileMd5", "fileSize"}
    assert p["fileType"] == p["multipartType"] == "0"
    assert set(p) == {"client_info", "curidentity", "req_time", "uniqid", "v", "fileType",
                      "multipartType", "source", "securityId"}
    part = single["files"][-1][1]
    assert part[0] == "图.jpg" and part[2] == "application/octet-stream"
    assert Image.open(io.BytesIO(part[1])).format == "JPEG"
    assert q["fileMd5"] == result["_upload"]["md5"]
    assert int(q["fileSize"]) == len(part[1]) == result["_upload"]["bytes"]
    assert not result["_upload"]["cached"]
    assert result["zpData"] == SUCCESS_DATA
    assert "Content-Type" not in single["headers"]  # requests must add its boundary


def test_precheck_hit_skips_file_upload(picture):
    c, calls = capture([response(SUCCESS_DATA)])
    out = c.upload_image(str(picture))
    assert len(calls) == 1 and not calls[0].get("files")
    assert out["_upload"]["cached"] and out["_upload"]["stage"] == "cache_hit"
    assert out["zpData"] == SUCCESS_DATA


def test_skip_precheck_and_upload_file_alias(picture):
    c, calls = capture([response(SUCCESS_DATA)])
    result = c.upload_file(str(picture), precheck=False, original=True)
    assert len(calls) == 1
    decode_call(calls[0])
    assert calls[0]["files"][-1][1][1] == picture.read_bytes()
    assert result["_upload"]["original"] is True


@pytest.mark.parametrize("key", [None, "", "<YOUR_ACCOUNT_SECRET_KEY>"])
def test_missing_secret_fails_before_network(picture, key):
    c, calls = capture([], {**SESSION, "secretKey": key})
    with pytest.raises(ValueError, match="secretKey"):
        c.upload_image(str(picture))
    assert calls == []


@pytest.mark.parametrize("field", ["t2", "uniqid", "client_info"])
def test_missing_other_session_fields_fail_before_network(picture, field):
    c, calls = capture([], {**SESSION, field: None})
    with pytest.raises(ValueError):
        c.upload_image(str(picture))
    assert calls == []


def test_requires_upload_context_not_search_card(picture):
    c, calls = capture([], {**SESSION, "upload_defaults": {},
                            "cardlist_defaults": {"securityId": "search-card-not-acceptable"}})
    with pytest.raises(ValueError, match="securityId"):
        c.upload_image(str(picture))
    assert not calls


def test_explicit_context_overrides_session(picture):
    c, calls = capture([response(SUCCESS_DATA)], {**SESSION, "upload_defaults": {"securityId": "other"}})
    c.upload_image(str(picture), CONTEXT)
    decode_call(calls[0])


@pytest.mark.parametrize("bad", [response(code=17), response(code=36),
    SimpleNamespace(status_code=200, content=b'{"zpData":{}}'),
    SimpleNamespace(status_code=200, content=b'{"code":0,"zpData":null}'),
    SimpleNamespace(status_code=200, content=b'{"code":0,"zpData":[]}'),
    response({"url": "only-one-url"}), response({"url": "", "tinyUrl": ""})])
def test_bad_precheck_never_continues_upload(picture, bad):
    c, calls = capture([bad])
    with pytest.raises(UploadError):
        c.upload_image(str(picture))
    assert len(calls) == 1


def test_expired_auth_is_preserved(picture):
    c, calls = capture([response(code=7)])
    with pytest.raises(AuthExpired):
        c.upload_image(str(picture))
    assert len(calls) == 1


def test_success_without_urls_is_not_reported_successful(picture):
    c, calls = capture([response()])
    with pytest.raises(UploadError, match="url/tinyUrl"):
        c.upload_image(str(picture), precheck=False)
    assert len(calls) == 1


def test_redirect_is_not_followed(picture):
    c, calls = capture([response(status=302)])
    with pytest.raises(UploadError, match="重定向"):
        c.upload_image(str(picture))
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False


def test_timeout_is_not_retried(picture):
    c = BossClient(dict(SESSION))
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise requests.Timeout("test")
    c.http.request = fail
    with pytest.raises(requests.Timeout):
        c.upload_image(str(picture), precheck=False)
    assert len(calls) == 1 and calls[0]["allow_redirects"] is False


def test_localhost_real_multipart_and_gzip(picture, monkeypatch):
    records = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            records.append((self.path, dict(self.headers), raw))
            obj = {} if self.path == UPLOAD_PRECHECK_PATH else SUCCESS_DATA
            content = gzip.compress(json.dumps({"code": 0, "zpData": obj}).encode())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    import bosscli.client as module
    monkeypatch.setattr(module, "UPLOAD_HOST", f"http://127.0.0.1:{server.server_port}")
    c = BossClient(dict(SESSION))
    c.http.trust_env = False  # no external proxy is ever used by this localhost test
    try:
        out = c.upload_image(str(picture))
    finally:
        c.http.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert out["zpData"] == SUCCESS_DATA
    assert len(records) == 2
    path, headers, raw = records[0]
    assert path == UPLOAD_PRECHECK_PATH
    quick = dict(parse_qsl(raw.decode(), keep_blank_values=True))
    assert list(quick) == ["sp", "sig", "app_id"]
    p, h, b = records[1]
    assert p == UPLOAD_IMAGE_PATH and "?" not in p
    msg = BytesParser(policy=default).parsebytes(
        ("MIME-Version: 1.0\r\nContent-Type: " + h["Content-Type"] + "\r\n\r\n").encode() + b)
    parts = list(msg.iter_parts())
    assert [x.get_param("name", header="content-disposition") for x in parts] == ["sp", "sig", "app_id", "file"]
    wire = {x.get_param("name", header="content-disposition"): x.get_payload(decode=True) for x in parts}
    params = dict(parse_qsl(yzwg.native_decode_content(quick["sp"], SECRET).decode(), keep_blank_values=True))
    import hashlib
    assert hashlib.md5(wire["file"]).hexdigest() == params["fileMd5"]
    assert str(len(wire["file"])) == params["fileSize"]
    assert parts[-1].get_content_type() == "application/octet-stream"
    assert all(int(x["Content-Length"]) == len(x.get_payload(decode=True)) for x in parts)
    for x in parts[:3]:
        assert x.get_filename() is None and x["Content-Type"] is None
    signed = yzwg.native_decode_content(wire["sp"].decode(), SECRET).decode()
    assert wire["sig"].decode() == yzwg.native_signature((UPLOAD_IMAGE_PATH + signed).encode(), SECRET)
    assert "fileSize" not in dict(parse_qsl(signed))


def test_image_exif_orientation_and_metadata_removal(tmp_path):
    p = tmp_path / "sideways.jpg"
    exif = Image.Exif()
    exif[274] = 6  # 90 degrees clockwise
    exif[270] = "private description"
    Image.new("RGB", (12, 8), "red").save(p, exif=exif)
    image = prepare_image(p)
    assert (image.width, image.height) == (8, 12)
    assert not Image.open(io.BytesIO(image.data)).getexif()
    assert "data=" not in repr(image)  # bytes do not leak into logs


def test_transparent_image_white_background(tmp_path):
    p = tmp_path / "alpha.png"
    Image.new("RGBA", (8, 12), (255, 0, 0, 0)).save(p)
    image = prepare_image(p)
    pixel = Image.open(io.BytesIO(image.data)).getpixel((0, 0))
    assert min(pixel) > 245


@pytest.mark.parametrize("raw", [b"", b"not an image", b"%PDF-not-an-image"])
def test_invalid_image_rejected(tmp_path, raw):
    p = tmp_path / "fake.png"
    p.write_bytes(raw)
    with pytest.raises(ValueError):
        prepare_image(p)


def test_animated_image_rejected(tmp_path):
    p = tmp_path / "animated.webp"
    frames = [Image.new("RGB", (8, 8), c) for c in ("red", "blue")]
    frames[0].save(p, format="WEBP", save_all=True, append_images=frames[1:], duration=100, loop=0)
    with pytest.raises(ValueError, match="多帧"):
        prepare_image(p)


def test_local_file_and_pixel_caps(tmp_path, monkeypatch):
    p = tmp_path / "huge.png"
    with p.open("wb") as f:
        f.truncate(MAX_IMAGE_BYTES + 1)
    with pytest.raises(ValueError, match="20 MiB"):
        prepare_image(p)
    Image.new("RGB", (10, 10)).save(p)
    import bosscli.upload as module
    monkeypatch.setattr(module, "MAX_IMAGE_PIXELS", 99)
    with pytest.raises(ValueError, match="像素"):
        prepare_image(p)


def test_cli_dry_run_never_reads_session_or_contacts_network(picture, capsys, monkeypatch):
    import bosscli.cli as cli
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run must not create a client")
    monkeypatch.setattr(cli, "_client", forbidden)
    cli.main(["--session", "does-not-exist.json", "upload", str(picture), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] and not out["sends_chat_message"]
    assert out["image"]["format"] == "JPEG" and out["precheck"]


def test_cli_context_file_and_confirm(picture, tmp_path, capsys, monkeypatch):
    import bosscli.cli as cli
    path = tmp_path / "context.txt"
    path.write_text(CONTEXT, encoding="utf-8-sig")
    c, calls = capture([response(SUCCESS_DATA)])
    monkeypatch.setattr(cli, "_client", lambda args: c)
    cli.main(["upload", str(picture), "--security-id-file", str(path), "--confirm", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["_upload"]["cached"] and len(calls) == 1
    assert CONTEXT not in json.dumps(out)


def test_cli_invalid_image_has_clear_error(tmp_path, capsys):
    import bosscli.cli as cli
    p = tmp_path / "bad.png"
    p.write_bytes(b"not an image")
    with pytest.raises(SystemExit) as exc:
        cli.main(["upload", str(p)])
    assert exc.value.code == 2
    assert "无法读取图片" in capsys.readouterr().err


def test_cli_security_id_options_are_mutually_exclusive(picture):
    import bosscli.cli as cli
    with pytest.raises(SystemExit) as exc:
        cli.main(["upload", str(picture), "--security-id", CONTEXT, "--security-id-file", "x"])
    assert exc.value.code == 2


@pytest.mark.parametrize("http_status", [401, 403])
def test_http_error_with_business_code7_still_reports_auth_expired(picture, http_status):
    c = BossClient(dict(SESSION))
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        r = requests.Response()
        r.status_code = http_status
        r._content = b'{"code":7,"message":"expired","zpData":{}}'
        raise requests.HTTPError("synthetic", response=r)
    c.http.request = fail
    with pytest.raises(AuthExpired):
        c.upload_image(str(picture))
    assert len(calls) == 1


def test_non_auth_http_error_does_not_continue(picture):
    c = BossClient(dict(SESSION))
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        r = requests.Response()
        r.status_code = 500
        r._content = b'not-json'
        raise requests.HTTPError("synthetic", response=r)
    c.http.request = fail
    with pytest.raises(requests.HTTPError):
        c.upload_image(str(picture))
    assert len(calls) == 1


def test_malformed_success_response_is_safe_unknown_result(picture):
    c, calls = capture([SimpleNamespace(status_code=200, content=b'not-an-encoded-body')])
    with pytest.raises(UploadError, match="无法解码"):
        c.upload_image(str(picture))
    assert len(calls) == 1


@pytest.mark.parametrize("error_type", [requests.exceptions.InvalidHeader, requests.ConnectionError, requests.Timeout])
def test_cli_transport_error_never_echoes_secret(picture, capsys, monkeypatch, error_type):
    import bosscli.cli as cli
    canary = "sensitive-canary-must-not-be-printed"
    class FakeClient:
        def _upload_prepared(self, *args, **kwargs):
            raise error_type(canary)
    monkeypatch.setattr(cli, "_client", lambda args: FakeClient())
    with pytest.raises(SystemExit) as exc:
        cli.main(["upload", str(picture), "--security-id", CONTEXT, "--confirm"])
    assert exc.value.code == 2
    out = capsys.readouterr()
    assert canary not in out.out + out.err and error_type.__name__ in out.err


def test_control_char_credentials_fail_without_echo(picture):
    canary = "secret-canary\nnot-a-valid-header"
    c, calls = capture([], {**SESSION, "t2": canary})
    with pytest.raises(ValueError) as exc:
        c.upload_image(str(picture))
    assert canary not in str(exc.value) and not calls


def test_16bit_grayscale_does_not_clip_midtones(tmp_path):
    p = tmp_path / "16bit.png"
    import struct
    values = [v for _ in range(10) for v in ([0] * 10 + [32768] * 10 + [65535] * 10)]
    image = Image.frombytes("I;16", (30, 10), struct.pack("<" + "H" * len(values), *values))
    image.save(p, format="PNG")
    prepared = prepare_image(p)
    result = Image.open(io.BytesIO(prepared.data))
    assert result.getpixel((4, 5))[0] < 10
    assert 115 <= result.getpixel((15, 5))[0] <= 140
    assert result.getpixel((25, 5))[0] > 245


def test_upload_envelope_handles_observed_5000_char_rule():
    params = {"very_long": "x" * 5100}
    enc = upload_envelope(params, UPLOAD_IMAGE_PATH, SECRET)
    raw = signer.build_query(params)
    assert yzwg.native_decode_content(enc["sp"], SECRET).decode() == raw
    assert enc["sig"] == yzwg.native_signature((UPLOAD_IMAGE_PATH + raw[:5000]).encode(), SECRET)
