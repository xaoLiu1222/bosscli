"""One selected job -> greeting -> image upload -> confirmed IM publish.

No automatic selection or bulk sends. Caller must present the bound job/image and obtain
explicit confirmation. The journal prevents blind retry after any potentially delivered write.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextlib import contextmanager

from .contacts import SelectedJob, parse_add_friend_response
from .upload import PreparedImage


class OutreachError(RuntimeError):
    """Safe workflow error, never including raw tokens or authorized image URLs."""


def attempt_key(uid: int, selected: SelectedJob, image: PreparedImage) -> str:
    raw = f"{uid}:{selected.user_id}:{selected.job_id}:".encode() + hashlib.sha256(image.data).digest()
    return hashlib.sha256(raw).hexdigest()


class Journal:
    """Local account/recipient/message-id journal; no t2/key/securityId or image bytes."""
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise OutreachError("已有沟通任务或遗留锁；请先核实，不能并行发送") from None
        try:
            os.close(fd)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def read(self):
        if not self.path.exists():
            return {"version": 1, "attempts": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise OutreachError("发送记录无法读取，已停止以免重复发送") from None
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("attempts"), dict):
            raise OutreachError("发送记录格式无效，已停止")
        return data

    def store(self, document):
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(document, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)


def execute_selected(client, sender, selected: SelectedJob, image: PreparedImage, *,
                     confirmed: bool = False, journal_path="outreach.local.json",
                     forbid_recontact: bool = False) -> dict:
    """No writes before confirmation + selected person/image validation + IM login.

    A failure after greeting_pending blocks automatic retry. Fixing/clearing such an entry
    requires human verification in the official app, never an automatic '--force' path.
    """
    if not confirmed:
        raise OutreachError("尚未确认所选岗位、招聘者和图片，不执行任何写操作")
    if not isinstance(selected, SelectedJob) or not isinstance(image, PreparedImage):
        raise OutreachError("必须绑定一个搜索结果和已准备的图片")
    uid = client.s.get("uid")
    if isinstance(uid, bool) or not str(uid).isdigit() or int(uid) <= 0:
        raise OutreachError("会话缺少有效账号 ID")
    if int(uid) == selected.user_id:
        raise OutreachError("不能向当前账号自身发送求职消息")
    if not client.s.get("t2") or not client.s.get("secretKey"):
        raise OutreachError("缺少当前账号登录凭据")
    im_session = getattr(sender, "session", None)
    if (not isinstance(im_session, dict) or str(im_session.get("uid")) != str(uid)
            or im_session.get("secretKey") != client.s["secretKey"]):
        raise OutreachError("HTTP 与聊天连接不是同一账号会话，已停止以免误发")
    journal = Journal(journal_path)
    identifier = attempt_key(int(uid), selected, image)
    with journal.locked():
        doc = journal.read()
        if identifier in doc["attempts"]:
            prior = doc["attempts"][identifier]
            if not isinstance(prior, dict):
                raise OutreachError("已有发送记录损坏，已停止以免重复发送")
            if (prior.get("stage") == "sent" and type(prior.get("server_mid")) is int
                    and prior["server_mid"] > 0 and type(prior.get("client_mid")) is int
                    and prior["client_mid"] > 0):
                return {"status": "already_sent", "client_mid": prior["client_mid"],
                        "server_mid": prior["server_mid"], "writes_performed": False}
            raise OutreachError("该岗位与图片已有未完成/不确定的发送记录；请先人工核实，禁止盲目重发")
        if forbid_recontact:
            # Check under the SAME lock as the irreversible attempt record, so a
            # concurrent sender cannot pass an earlier recruiter-only snapshot.
            for prior_record in doc["attempts"].values():
                if not isinstance(prior_record, dict):
                    raise OutreachError("已有发送记录损坏，已停止以免重复联系")
                if (str(prior_record.get("uid")) == str(uid)
                        and str(prior_record.get("friend_id")) == str(selected.user_id)):
                    raise OutreachError("同一招聘者已存在发送或不确定记录，已停止重复联系")
        # This can displace another same-client IM connection: included in user confirmation.
        sender.open()
        try:
            record = {"uid": int(uid), "friend_id": selected.user_id, "job_id": selected.job_id,
                      "image_sha256": hashlib.sha256(image.data).hexdigest(),
                      "client_mid": sender.new_client_mid(), "stage": "greeting_pending"}
            doc["attempts"][identifier] = record
            journal.store(doc)  # persist BEFORE sending a potentially irreversible request
            context = parse_add_friend_response(selected, client.add_friend(**selected.add_friend_kwargs()))
            record["stage"] = "greeted"
            journal.store(doc)
            upload = client._upload_prepared(image, context.security_id)
            record["stage"] = "uploaded"
            journal.store(doc)
            record["stage"] = "message_pending"
            journal.store(doc)
            server_mid = sender.send_image(context, upload, client_mid=record["client_mid"])
            if isinstance(server_mid, bool) or not isinstance(server_mid, int) or server_mid <= 0:
                raise OutreachError("没有收到匹配的图片消息业务回执")
            record.update(stage="sent", server_mid=server_mid)
            journal.store(doc)
            return {"status": "sent", "client_mid": record["client_mid"], "server_mid": server_mid,
                    "writes_performed": True, "job": selected.name, "company": selected.company,
                    "recipient": selected.recipient_name}
        except Exception as exc:
            # Preserve the last durable stage; never turn a timeout into 'not sent'.
            if identifier in doc["attempts"]:
                record["last_error_type"] = type(exc).__name__
                journal.store(doc)
            raise
        finally:
            sender.close()
