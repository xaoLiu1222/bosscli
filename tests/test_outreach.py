"""Synthetic outreach safety tests: no session files, real recipients or network."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bosscli.contacts import ContactError, SelectedJob
from bosscli import outreach
from bosscli.outreach import Journal, OutreachError, attempt_key, execute_selected
from bosscli.upload import PreparedImage


SEARCH_TOKEN = "synthetic-search-token-not-chat"
CHAT_TOKEN = "synthetic-relation-chat-token"
SECRET = "synthetic-account-secret"
T2 = "synthetic-account-t2"
LID = "synthetic-selected-tracking-id"
# Fixed one-pixel PNG; no runtime image encoder or user image is involved.
IMAGE_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082")


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("synthetic outreach tests must never contact a network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def selected():
    return SelectedJob.from_search_job({"name": "Synthetic Engineer", "company": "Synthetic Co",
        "jobId": 100001, "userId": 200002, "securityId": SEARCH_TOKEN,
        "lid": LID, "hr": "Synthetic Recruiter"})


@pytest.fixture
def image():
    return PreparedImage(IMAGE_BYTES, "synthetic.png", 1, 1, "PNG", True)


def relation(**updates):
    # All source/security fields are explicit; a fake must honor the real binding contract.
    return {"friendId": 200002, "jobId": 100001, "securityId": CHAT_TOKEN,
            "friendSource": 2, "userSource": 1, "jobSource": 7, **updates}


class FakeClient:
    def __init__(self, events, *, uid=300003, response=None, fail=None):
        self.events, self.fail = events, fail
        self.s = {"uid": uid, "t2": T2, "secretKey": SECRET}
        self.response = {"code": 0, "zpData": {"relation": relation()}} if response is None else response
        self.greetings, self.uploads = [], []
        self.before_greet = self.before_upload = lambda: None

    def add_friend(self, **kwargs):
        self.before_greet()
        self.events.append("greet")
        self.greetings.append(kwargs)
        if self.fail == "greet":
            raise TimeoutError("synthetic failure " + SECRET)
        return deepcopy(self.response)

    def _upload_prepared(self, image, security_id):
        self.before_upload()
        self.events.append("upload")
        self.uploads.append((image, security_id))
        if self.fail == "upload":
            raise TimeoutError("synthetic failure " + SECRET)
        return {"code": 0, "zpData": {"url": "https://img.bosszhipin.com/synthetic-origin",
                "tinyUrl": "https://img.bosszhipin.com/synthetic-tiny"}}


class FakeSender:
    def __init__(self, events, *, fail=None, server_mid=900009, uid=300003):
        self.events, self.fail, self.server_mid = events, fail, server_mid
        self.session = {"uid": uid, "secretKey": SECRET}
        self.calls = []
        self.before_send = lambda: None

    def open(self):
        self.events.append("open")
        if self.fail == "open":
            raise ConnectionError("synthetic login rejected")

    def new_client_mid(self):
        self.events.append("mid")
        if self.fail == "mid":
            raise ValueError("synthetic mid rejected")
        return 800008

    def send_image(self, context, upload, *, client_mid):
        self.before_send()
        self.events.append("send")
        self.calls.append((context, upload, client_mid))
        if self.fail == "send":
            raise TimeoutError("synthetic delivery unknown " + SECRET)
        return self.server_mid

    def close(self):
        self.events.append("close")


def read_record(path, uid, selected, image):
    document = json.loads(path.read_text(encoding="utf-8"))
    return document["attempts"][attempt_key(uid, selected, image)]


@pytest.mark.parametrize("confirmed", [False, None, 0])
def test_unconfirmed_does_not_even_open_or_create_journal(tmp_path, selected, image, confirmed):
    events = []
    client, sender = FakeClient(events), FakeSender(events)
    path = tmp_path / "not-created" / "journal.json"
    with pytest.raises(OutreachError, match="尚未确认"):
        execute_selected(client, sender, selected, image, confirmed=confirmed, journal_path=path)
    assert events == [] and client.greetings == [] and client.uploads == []
    assert not path.parent.exists()


@pytest.mark.parametrize("prior_stage", ["sent", "message_pending", "greeting_pending"])
def test_recruiter_recontact_is_blocked_inside_journal_lock(tmp_path, selected, image, prior_stage):
    events = []
    path = tmp_path / "journal.json"
    prior = {"uid": 300003, "friend_id": selected.user_id, "job_id": 999999,
             "image_sha256": "different-image", "client_mid": 700007, "server_mid": 900009,
             "stage": prior_stage}
    Journal(path).store({"version": 1, "attempts": {"another-key": prior}})
    with pytest.raises(OutreachError, match="同一招聘者"):
        execute_selected(FakeClient(events), FakeSender(events), selected, image,
                         confirmed=True, journal_path=path, forbid_recontact=True)
    assert events == []
    assert len(Journal(path).read()["attempts"]) == 1


def test_unconfirmed_checks_before_dereferencing_arguments(tmp_path):
    with pytest.raises(OutreachError, match="尚未确认"):
        execute_selected(None, None, None, None, journal_path=tmp_path / "journal.json")


@pytest.mark.parametrize("bad_selected,bad_image", [(None, None), ({}, None), ([], None), (None, b"bytes")])
def test_unbound_arguments_fail_before_open(tmp_path, bad_selected, bad_image):
    events = []
    with pytest.raises(OutreachError, match="必须绑定"):
        execute_selected(FakeClient(events), FakeSender(events), bad_selected, bad_image,
                         confirmed=True, journal_path=tmp_path / "journal.json")
    assert events == []


@pytest.mark.parametrize("uid", [None, True, False, 0, -1, "", "1.0", "x", 1.2, 200002])
def test_bad_account_or_self_target_does_not_open_or_greet(tmp_path, selected, image, uid):
    events = []
    with pytest.raises(OutreachError):
        execute_selected(FakeClient(events, uid=uid), FakeSender(events), selected, image,
                         confirmed=True, journal_path=tmp_path / "journal.json")
    assert events == []


@pytest.mark.parametrize("name", ["t2", "secretKey"])
@pytest.mark.parametrize("missing", [True, False])
def test_missing_credentials_fail_before_im_login(tmp_path, selected, image, name, missing):
    events = []
    client = FakeClient(events)
    if missing:
        del client.s[name]
    else:
        client.s[name] = ""
    with pytest.raises(OutreachError, match="登录凭据"):
        execute_selected(client, FakeSender(events), selected, image, confirmed=True,
                         journal_path=tmp_path / "journal.json")
    assert events == []


def test_success_persists_each_stage_before_irreversible_call_and_keeps_image_fixed(
        monkeypatch, tmp_path, selected, image):
    events, snapshots = [], []
    path = tmp_path / "journal.json"
    client, sender = FakeClient(events), FakeSender(events)
    original_store = Journal.store

    def store(journal, document):
        original_store(journal, document)
        snapshot = json.loads(journal.path.read_text(encoding="utf-8"))
        record = snapshot["attempts"][attempt_key(300003, selected, image)]
        snapshots.append(snapshot)
        events.append("persist:" + record["stage"])
    monkeypatch.setattr(Journal, "store", store)

    def durable(stage):
        assert read_record(path, 300003, selected, image)["stage"] == stage
        assert path.with_suffix(".json.lock").is_file()
    client.before_greet = lambda: durable("greeting_pending")
    client.before_upload = lambda: durable("greeted")
    sender.before_send = lambda: durable("message_pending")
    result = execute_selected(client, sender, selected, image, confirmed=True, journal_path=path)

    assert result == {"status": "sent", "client_mid": 800008, "server_mid": 900009,
        "writes_performed": True, "job": "Synthetic Engineer", "company": "Synthetic Co",
        "recipient": "Synthetic Recruiter"}
    assert events == ["open", "mid", "persist:greeting_pending", "greet", "persist:greeted",
                      "upload", "persist:uploaded", "persist:message_pending", "send", "persist:sent", "close"]
    assert client.greetings == [{"security_id": SEARCH_TOKEN, "job_id": "100001",
                                "friend_id": "200002", "lid": LID}]
    assert client.uploads == [(image, CHAT_TOKEN)]
    assert client.uploads[0][0] is image and image.data == IMAGE_BYTES
    context, upload, cmid = sender.calls[0]
    assert context.selected_job is selected
    assert (context.friend_id, context.job_id, context.friend_source, context.user_source, context.job_source) == (
        200002, 100001, 65538, 1, 7)
    assert context.security_id == CHAT_TOKEN and context.security_id != SEARCH_TOKEN
    assert cmid == 800008 and upload["code"] == 0
    assert len(snapshots) == 5
    assert snapshots[0]["attempts"][attempt_key(300003, selected, image)]["stage"] == "greeting_pending"
    assert not path.with_suffix(".json.lock").exists()
    text = path.read_text(encoding="utf-8")
    for secret in (SEARCH_TOKEN, CHAT_TOKEN, SECRET, T2, LID, "https://", IMAGE_BYTES.hex()):
        assert secret not in text
    assert read_record(path, 300003, selected, image)["image_sha256"] == hashlib.sha256(IMAGE_BYTES).hexdigest()


@pytest.mark.parametrize("where,stage,expected", [
    ("greet", "greeting_pending", ["open", "mid", "greet", "close"]),
    ("upload", "greeted", ["open", "mid", "greet", "upload", "close"]),
    ("send", "message_pending", ["open", "mid", "greet", "upload", "send", "close"])])
def test_write_failure_keeps_uncertainty_and_blocks_every_automatic_retry(tmp_path, selected, image, where, stage, expected):
    events = []
    client = FakeClient(events, fail=where)
    sender = FakeSender(events, fail=where)
    path = tmp_path / "journal.json"
    with pytest.raises(TimeoutError):
        execute_selected(client, sender, selected, image, confirmed=True, journal_path=path)
    assert events == expected
    record = read_record(path, 300003, selected, image)
    assert record["stage"] == stage and record["last_error_type"] == "TimeoutError"
    assert SECRET not in path.read_text(encoding="utf-8")
    for _ in range(2):
        retry_events = []
        with pytest.raises(OutreachError, match="禁止盲目重发"):
            execute_selected(FakeClient(retry_events), FakeSender(retry_events), selected, image,
                             confirmed=True, journal_path=path)
        assert retry_events == []
    assert not path.with_suffix(".json.lock").exists()


@pytest.mark.parametrize("failure", ["open", "mid"])
def test_sender_login_or_mid_failure_never_greets(tmp_path, selected, image, failure):
    events = []
    client, sender = FakeClient(events), FakeSender(events, fail=failure)
    path = tmp_path / "journal.json"
    with pytest.raises((ConnectionError, ValueError)):
        execute_selected(client, sender, selected, image, confirmed=True, journal_path=path)
    assert events == (["open"] if failure == "open" else ["open", "mid", "close"])
    assert client.greetings == [] and not path.exists()
    assert not path.with_suffix(".json.lock").exists()


@pytest.mark.parametrize("persist_number,forbidden_event", [(1, "greet"), (2, "upload"), (3, "send"), (4, "send")])
def test_persistence_failure_never_advances_to_next_write(monkeypatch, tmp_path, selected, image, persist_number, forbidden_event):
    original_store = Journal.store
    count = 0
    def fail_once(journal, document):
        nonlocal count
        count += 1
        if count == persist_number:
            raise OSError("synthetic disk failure")
        original_store(journal, document)
    monkeypatch.setattr(Journal, "store", fail_once)
    events = []
    path = tmp_path / "journal.json"
    with pytest.raises(OSError):
        execute_selected(FakeClient(events), FakeSender(events), selected, image,
                         confirmed=True, journal_path=path)
    assert forbidden_event not in events and events[-1] == "close"
    assert not path.with_suffix(".json.lock").exists()
    with pytest.raises(OutreachError, match="禁止盲目重发"):
        execute_selected(FakeClient([]), FakeSender([]), selected, image,
                         confirmed=True, journal_path=path)


@pytest.mark.parametrize("bad_mid", [None, False, True, 0, -1, "900009", 1.2])
def test_non_business_receipt_never_becomes_sent_or_retries(tmp_path, selected, image, bad_mid):
    events = []
    path = tmp_path / "journal.json"
    with pytest.raises(OutreachError, match="业务回执"):
        execute_selected(FakeClient(events), FakeSender(events, server_mid=bad_mid), selected, image,
                         confirmed=True, journal_path=path)
    assert events.count("send") == 1
    assert read_record(path, 300003, selected, image)["stage"] == "message_pending"
    retry = []
    with pytest.raises(OutreachError, match="禁止盲目重发"):
        execute_selected(FakeClient(retry), FakeSender(retry), selected, image,
                         confirmed=True, journal_path=path)
    assert retry == []


@pytest.mark.parametrize("updates", [{"friendId": 200003}, {"jobId": 100002}])
def test_returned_relation_cannot_retarget_selected_receiver_or_job(tmp_path, selected, image, updates):
    events = []
    client = FakeClient(events, response={"code": 0, "zpData": {"relation": relation(**updates)}})
    path = tmp_path / "journal.json"
    with pytest.raises(ContactError):
        execute_selected(client, FakeSender(events), selected, image, confirmed=True, journal_path=path)
    assert events == ["open", "mid", "greet", "close"]
    assert client.uploads == []
    assert read_record(path, 300003, selected, image)["stage"] == "greeting_pending"


@pytest.mark.parametrize("field", ["friendId", "friendSource", "userSource", "jobSource", "securityId"])
def test_relation_missing_source_security_or_friend_never_falls_back(tmp_path, selected, image, field):
    bean = relation()
    del bean[field]
    events = []
    response = {"code": 0, "zpData": {"relation": bean, "securityId": SEARCH_TOKEN}}
    with pytest.raises(ContactError, match=field):
        execute_selected(FakeClient(events, response=response), FakeSender(events), selected, image,
                         confirmed=True, journal_path=tmp_path / "journal.json")
    assert events == ["open", "mid", "greet", "close"]


@pytest.mark.parametrize("response", [{"code": 5, "message": SECRET}, {"code": "0", "zpData": {}},
    {"code": False, "zpData": {}}, {"code": 0, "zpData": {}},
    {"code": 0, "zpData": {"baseInfoList": [relation()]}}])
def test_failed_or_wrong_greeting_envelope_never_uploads(tmp_path, selected, image, response):
    events = []
    with pytest.raises(ContactError) as exc:
        execute_selected(FakeClient(events, response=response), FakeSender(events), selected, image,
                         confirmed=True, journal_path=tmp_path / "journal.json")
    assert SECRET not in str(exc.value)
    assert events == ["open", "mid", "greet", "close"]


def test_sent_duplicate_same_account_job_and_bytes_returns_before_open(tmp_path, selected, image):
    path = tmp_path / "journal.json"
    execute_selected(FakeClient([]), FakeSender([]), selected, image, confirmed=True, journal_path=path)
    before = path.read_bytes()
    events = []
    same_target = replace(selected, name="Changed title", company="Changed label",
                          recipient_name="Changed synthetic name", search_security_id="different-search-token", lid="different-lid")
    same_bytes = replace(image, filename="renamed.png", original=False)
    result = execute_selected(FakeClient(events), FakeSender(events), same_target, same_bytes,
                              confirmed=True, journal_path=path)
    assert result == {"status": "already_sent", "client_mid": 800008, "server_mid": 900009,
                      "writes_performed": False}
    assert events == [] and path.read_bytes() == before


def test_different_account_has_a_distinct_attempt_and_can_send(tmp_path, selected, image):
    path = tmp_path / "journal.json"
    for uid in (300003, 300004):
        events = []
        result = execute_selected(FakeClient(events, uid=uid), FakeSender(events, uid=uid), selected, image,
                                  confirmed=True, journal_path=path)
        assert result["status"] == "sent" and events.count("send") == 1
    attempts = json.loads(path.read_text(encoding="utf-8"))["attempts"]
    assert len(attempts) == 2 and {record["uid"] for record in attempts.values()} == {300003, 300004}


def test_attempt_key_binds_account_recipient_job_and_exact_bytes_not_filename(selected, image):
    baseline = attempt_key(300003, selected, image)
    assert len(baseline) == 64 and int(baseline, 16) >= 0
    assert baseline == attempt_key(300003, replace(selected, name="Different display"), replace(image, filename="other.png"))
    assert len({baseline, attempt_key(300004, selected, image),
                attempt_key(300003, replace(selected, user_id=200003), image),
                attempt_key(300003, replace(selected, job_id=100002), image),
                attempt_key(300003, selected, replace(image, data=IMAGE_BYTES + b"x"))}) == 5


@pytest.mark.parametrize("mismatch", ["uid", "key", "missing_session"])
def test_sender_and_http_client_must_be_same_account_before_any_write(tmp_path, selected, image, mismatch):
    events = []
    sender = FakeSender(events)
    if mismatch == "uid":
        sender.session["uid"] = 300004
    elif mismatch == "key":
        sender.session["secretKey"] = "synthetic-different-account-key"
    else:
        del sender.session
    with pytest.raises(OutreachError):
        execute_selected(FakeClient(events), sender, selected, image,
                         confirmed=True, journal_path=tmp_path / "journal.json")
    assert events == []


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"version":2,"attempts":{}}',
                                 '{"version":1,"attempts":[]}'])
def test_corrupt_journal_fails_closed_before_open(tmp_path, selected, image, raw):
    path = tmp_path / "journal.json"
    path.write_text(raw, encoding="utf-8")
    events = []
    with pytest.raises(OutreachError, match="发送记录"):
        execute_selected(FakeClient(events), FakeSender(events), selected, image, confirmed=True, journal_path=path)
    assert events == [] and path.read_text(encoding="utf-8") == raw
    assert not path.with_suffix(".json.lock").exists()


@pytest.mark.parametrize("prior", [None, {}, [], "", 0, "synthetic-corrupt-entry",
    {"stage": "greeting_pending"}, {"stage": "greeted"}, {"stage": "uploaded"},
    {"stage": "message_pending"}, {"stage": "unknown"},
    {"stage": "sent", "client_mid": 800008},
    {"stage": "sent", "client_mid": 800008, "server_mid": 0},
    {"stage": "sent", "client_mid": 800008, "server_mid": -1},
    {"stage": "sent", "client_mid": 800008, "server_mid": True},
    {"stage": "sent", "client_mid": 800008, "server_mid": "900009"},
    {"stage": "sent", "client_mid": True, "server_mid": 900009},
    {"stage": "sent", "server_mid": 900009}])
def test_existing_incomplete_or_malformed_attempt_never_opens_or_retries(tmp_path, selected, image, prior):
    path = tmp_path / "journal.json"
    document = {"version": 1, "attempts": {attempt_key(300003, selected, image): prior}}
    path.write_text(json.dumps(document), encoding="utf-8")
    previous = path.read_bytes()
    events = []
    with pytest.raises(OutreachError):
        execute_selected(FakeClient(events), FakeSender(events), selected, image,
                         confirmed=True, journal_path=path)
    assert events == [] and path.read_bytes() == previous
    assert not path.with_suffix(".json.lock").exists()


def test_preexisting_lock_blocks_without_deleting_someone_elses_lock(tmp_path, selected, image):
    path = tmp_path / "journal.json"
    lock = path.with_suffix(".json.lock")
    lock.write_text("synthetic existing lock", encoding="utf-8")
    events = []
    with pytest.raises(OutreachError, match="不能并行发送"):
        execute_selected(FakeClient(events), FakeSender(events), selected, image, confirmed=True, journal_path=path)
    assert events == [] and lock.read_text(encoding="utf-8") == "synthetic existing lock"


def test_journal_atomic_store_flushes_before_replace_and_cleans_failed_temp(monkeypatch, tmp_path):
    path = tmp_path / "journal.json"
    journal = Journal(path)
    original_replace, original_fsync = outreach.os.replace, outreach.os.fsync
    events = []
    def fsync(fd):
        events.append("fsync")
        original_fsync(fd)
    def replace_file(source, target):
        assert events[-1] == "fsync" and Path(target) == path
        events.append("replace")
        original_replace(source, target)
    monkeypatch.setattr(outreach.os, "fsync", fsync)
    monkeypatch.setattr(outreach.os, "replace", replace_file)
    journal.store({"version": 1, "attempts": {}})
    assert events == ["fsync", "replace"]
    previous = path.read_bytes()
    def fail_replace(*args):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(outreach.os, "replace", fail_replace)
    with pytest.raises(OSError):
        journal.store({"version": 1, "attempts": {"synthetic": {"stage": "sent"}}})
    assert path.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []
