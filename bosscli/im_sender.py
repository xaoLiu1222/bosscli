"""Pure-Python image-message sender. No ADB, injected code or automatic reconnect.

Runtime TLS material is provisioned locally, never embedded. A publish is successful only
with a matching extended PUBACK or protobuf messageSync; timeout means UNKNOWN delivery.
"""
from __future__ import annotations

import math
import socket
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

from .contacts import ConversationContext
from .im_mqtt import (MQTTConnection, MQTTError, MQTTTimeout, TransportAckOnly,
                      decode_publish, make_auth)
from .im_proto import ImageInfo, business_ack, encode_image_message, integer


class ImageSender:
    def __init__(self, session: dict, *, connection_factory=MQTTConnection):
        self.session = session
        self._factory = connection_factory
        self.connection = None
        self._last_mid = 0
        self.validate_configuration()

    def validate_configuration(self):
        config = self.session.get("im")
        if not isinstance(config, dict):
            raise ValueError("缺少 session.im 聊天连接配置，不能开始打招呼或发图")
        host = config.get("host", "chat.zhipin.com")
        port = config.get("port", 443)
        if host not in ("chat.zhipin.com", "mqtt-ecc.zhipin.com") or type(port) is not int or port != 443:
            raise ValueError("当前仅支持已核实域名的 TLS:443 聊天通道")
        for name in ("ca_file", "cert_file", "key_file"):
            value = config.get(name)
            if not isinstance(value, str) or not Path(value).is_file():
                raise ValueError(f"session.im.{name} 未配置或文件不存在")
        def decimal(value, name):
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError(f"{name} 必须是十进制整数")
            if isinstance(value, str) and (not value.isascii() or not value.isdecimal()):
                raise ValueError(f"{name} 必须是十进制整数")
            return int(value)
        integer(decimal(self.session.get("uid"), "uid"), "account uid", 1)
        key = self.session.get("secretKey")
        if not isinstance(key, str) or not key or any(ord(c) < 32 for c in key) or key.startswith("<"):
            raise ValueError("缺少有效账号 secretKey")
        role = decimal(self.session.get("curidentity", 0), "role")
        if role != 0:
            raise ValueError("本流程只支持求职者账号")
        version = self.session.get("v", "14.180")
        device_id = config.get("device_id", self.session.get("uniqid"))
        if not isinstance(version, str) or not version or any(ord(c) < 32 for c in version):
            raise ValueError("缺少有效 App 版本")
        if device_id is not None and not isinstance(device_id, str):
            raise ValueError("聊天设备标识应为字符串")
        timeout = config.get("timeout", 15)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
            raise ValueError("聊天超时须为 1–60 秒")
        self.config = config
        self.host, self.port, self.timeout = host, port, timeout
        # Build/validate TLS before ANY greeting/upload write is attempted.
        self._context = ssl.create_default_context(cafile=config["ca_file"])
        self._context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._context.load_cert_chain(config["cert_file"], config["key_file"])

    def open(self):
        if self.connection is not None:
            raise MQTTError("IM connection already open")
        cfg = self.config
        auth = make_auth(self.session["uid"], self.session.get("curidentity", "0"),
                         self.session.get("v", "14.180"), cfg.get("device_id", self.session.get("uniqid")),
                         self.session["secretKey"])
        connection = self._factory(socket_factory=socket.create_connection, host=self.host, port=self.port,
                                   ssl_context=self._context, timeout=self.timeout)
        try:
            connection.connect(auth)
            connection.subscribe_chat()
        except Exception:
            connection.close()
            raise
        self.connection = connection

    def new_client_mid(self) -> int:
        self._last_mid = max(int(time.time() * 1000), self._last_mid + 1)
        return self._last_mid

    def send_image(self, context: ConversationContext, upload_result: dict, *, client_mid: int) -> int:
        if not isinstance(context, ConversationContext) or self.connection is None:
            raise ValueError("已绑定的会话与已登录的聊天连接不可缺少")
        integer(client_mid, "client message id", 1)
        data = upload_result.get("zpData") or {}
        local = upload_result.get("_upload") or {}
        metadata = data.get("metadata") or {}
        width = metadata.get("width", local.get("width"))
        height = metadata.get("height", local.get("height"))
        origin = ImageInfo(data.get("url"), width, height)
        # Do not guess thumbnail dimensions or dereference an arbitrary returned URL.
        tiny_width, tiny_height = data.get("tinyWidth"), data.get("tinyHeight")
        if not tiny_width or not tiny_height:
            tiny_width, tiny_height = self._thumbnail_size(data.get("tinyUrl"))
        tiny = ImageInfo(data.get("tinyUrl"), tiny_width, tiny_height)
        payload = encode_image_message(from_uid=int(self.session["uid"]), to_uid=context.friend_id,
                                       client_mid=client_mid, origin=origin, tiny=tiny,
                                       from_source=context.user_source,
                                       to_source=context.friend_source & 0xffff,
                                       sender_name=self.session.get("name", ""))
        started = time.monotonic()
        try:
            return self.connection.publish(payload, client_mid, timeout=self.timeout)
        except MQTTTimeout:
            # A business ACK may already have arrived while waiting for a lost PUBACK.
            # Inspect ONLY buffered packets; no retry, reconnect or extended deadline.
            pop = getattr(self.connection, "pop_pending", None)
            if pop is not None:
                for _ in range(128):
                    packet = pop()
                    if packet is None:
                        break
                    if packet.packet_type == 3:
                        incoming = decode_publish(packet)
                        if incoming.topic == "chat":
                            server_mid = business_ack(incoming.payload, client_mid)
                            if server_mid is not None:
                                return server_mid
            raise
        except TransportAckOnly:
            # A bare MQTT ACK is not delivery confirmation. Consume queued/new protobuf ACKs.
            for _ in range(128):
                left = self.timeout - (time.monotonic() - started)
                if left <= 0:
                    raise MQTTTimeout("图片传输已确认，但未收到业务消息ID；请勿盲目重发")
                packet = self.connection.receive(timeout=left)
                if packet.packet_type == 3:
                    incoming = decode_publish(packet)
                    if incoming.topic != "chat":
                        continue
                    server_mid = business_ack(incoming.payload, client_mid)
                    if server_mid is not None:
                        return server_mid
            raise MQTTError("接收数据过多，仍未确认图片消息发送")

    @staticmethod
    def _thumbnail_size(url: str) -> tuple[int, int]:
        """Only fetch a thumbnail from the observed image CDN, bounded and no redirects."""
        import io
        import requests
        from PIL import Image
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme != "https" or parsed.hostname not in
                ("imgaz.bosszhipin.com", "img.bosszhipin.com", "img2.bosszhipin.com")
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError("缩略图地址不属于已核实的 HTTPS 图片域名")
        try:
            with requests.get(url, timeout=10, allow_redirects=False, stream=True) as response:
                if response.status_code != 200:
                    raise ValueError("无法读取已上传图片的缩略图尺寸")
                data = bytearray()
                for piece in response.iter_content(32768):
                    data.extend(piece)
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError("缩略图超过本地大小限制")
                with Image.open(io.BytesIO(data)) as image:
                    if image.width * image.height > 4_000_000:
                        raise ValueError("缩略图超过本地像素限制")
                    return image.size
        except requests.RequestException as exc:
            raise ValueError("无法读取缩略图，请先核实上传结果，不要重复发送") from exc

    def close(self):
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.disconnect(timeout=2)
            except Exception:
                connection.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()
