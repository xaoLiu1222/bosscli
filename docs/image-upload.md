# 图片上传（不发送聊天消息）

本功能将 **2026-11-11 已实测的 App 图片上传协议**接入 bosscli，只上传图片并返回地址。
**拿到 `url` 不代表对方已收到图片**：不会发送 IM 图片消息、调用打招呼接口，也不会自动拿用户的真实简历做在线验证。
协议证据、旧原型的纠正与验证边界见[上传协议记录](upload-capture.md)。

## CLI：默认纯本地预览

```bash
# 不需要 token；只读取、校验和预处理本地图片，不访问上传或预检接口
bosscli upload ./image.png
bosscli upload ./image.png --json

# 明确确认后才访问服务器；securityId 从受保护的本地文件读取
bosscli --session ./session.local.json upload ./image.png --security-id-file ./upload-security-id.txt --confirm --json

# 也可显式传入上传上下文的 securityId；注意命令行参数可能留在 shell 历史中
bosscli --session ./session.local.json upload ./image.png --security-id UPLOAD_SECURITY_ID --confirm

# 保留原文件字节；或跳过 MD5 预检直接上传（仍需 --confirm 才联网）
bosscli --session ./session.local.json upload ./image.webp --original --confirm
bosscli --session ./session.local.json upload ./image.jpg --skip-precheck --confirm
```

主命令形式为 `bosscli [--session PATH] upload IMAGE`。最后两例要求 session 已配置正确的 `upload_defaults.securityId`。

| 选项 | 含义 |
|---|---|
| `--confirm` | 才真正上传；不传时是纯本地 dry-run，即使缺少 token 也可预览。 |
| `--security-id TOKEN` | 显式指定**上传上下文**的 `securityId`。 |
| `--security-id-file PATH` | 从本地文件读取上述 token；与 `--security-id` 互斥。 |
| `--skip-precheck` | 跳过默认的 MD5 秒传预检；dry-run 下无论是否指定都不会发预检请求。 |
| `--original` | 保留原文件字节，不重新编码；格式、单帧及大小/像素保护仍适用。 |
| `--json` | JSON 输出包含 `code`、`message`、`zpData` 以及本地 `_upload` 元数据。 |
| `--token TOKEN` | 覆盖 session 的 `t2`，不能替代 `secretKey` 或上传 `securityId`。 |

`securityId` 取值顺序：显式的 `--security-id` / `--security-id-file` →
`session.upload_defaults.securityId`。**不要把职位搜索卡片的 `securityId` 任意替代为上传 token。**
实际上传前需准备有效的上传上下文；本地预览不要求具备完整的在线凭据。

在线请求需要 session 中的账号 `secretKey`，以及有效的 `t2`、`client_info`、`uniqid` 等会话/设备参数。
凭据应由使用者自行配置到受保护的本地 session；禁止硬编码真实凭据、从抓包自动导入，或写入测试 fixtures。
`--token` 和直接传入的 `--security-id` 都可能被进程列表或 shell 历史记录，优先使用受保护的本地配置/文件。
不要把带 token、签名参数、图片内容或个人简历的完整日志发布到 issue。

## 图片预处理与本地保护

当前只支持 **JPEG / PNG / WebP 单帧图片**，不支持任意文件、PDF、动画或多帧图片。

- 默认调用 `prepare_image`：按 EXIF 方向转正，透明区域合成到白底，再转换为 RGB JPEG。
- JPEG `quality=90` 是 **bosscli 的本地策略**，不是已证明的 Android 编码配置，不能据此声称与 App 上传字节相同。
- `--original` / `original=True` 保留原文件字节；MD5 和大小均以本次实际准备上传的字节计算。
- **20 MiB 文件大小、40M（40,000,000）像素**是本地安全保护，不是已测得的服务端限制，也不保证保护范围内的所有图片都会被服务端接受。
- `prepare_image` 使用的内部图片 `bytes` 不得泄漏到日志或 JSON。`_upload` 是本地处理元数据，不是服务端字段，也不应包含凭据或文件内容。

默认转码后，上传 MD5/大小可能与输入文件不同。即使使用 `--original`，服务器接收后返回的图片 metadata
也可能与上传字节的 MD5/大小不同；不要据此判定传输失败，也不要把服务端 metadata 当作原文件校验和。

## Python API

```python
# client 是已经配置好会话的 BossClient 实例。
# Python API 是实际上传调用，不具有 CLI 的默认 dry-run 保护。
result = client.upload_image("image.png", security_id="UPLOAD_SECURITY_ID")

# 不做预检并保留原始字节
result = client.upload_image(
    "image.webp", security_id="UPLOAD_SECURITY_ID", precheck=False, original=True
)
```

公开接口：

```python
BossClient.upload_image(path, security_id=None, *, precheck=True, original=False) -> dict
```

`security_id=None` 时使用 `session.upload_defaults.securityId`。
`upload_file` 是兼容别名，**也只支持图片**；不再接受旧原型猜测的 `route`、`part`、`field`/`fields`、`key` 参数。
`prepare_image` 为本地预处理 helper，不承担发送消息或自动读取抓包凭据的功能。

## 请求协议

固定 host：`https://api-and.zhipin.com`。以下业务字段放进加密的 `sp`，**不把原始字段放到 query**。
两步请求均使用当前账号的 `secretKey` 签名；`sig` 不包含 `strA`，`app_id` 在签名完成后追加。

### 1. MD5 预检：`POST /api/zpupload/quicklyUpload`

默认启用；正文为 `application/x-www-form-urlencoded`，**只有 `sp`、`sig`、`app_id` 三个字段**。
`sp` 中包含 commons（会话/设备公共字段）及：

```text
source = "chat_file"
securityId = 上传上下文的 token
gid = ""
fileMd5 = 实际上传字节的 MD5
fileSize = 实际上传字节数
```

成功响应的 `zpData` 中 **`url` 和 `tinyUrl` 两者都非空**才算 cache hit：直接返回地址，不再上传文件。
成功响应的 `zpData` 为空则继续 `uploadSingle`。不能只因 `code=0` 就推断秒传命中。
预检返回非零业务码、畸形响应或只有一个地址的 partial cache 时，报错停止，**不会继续上传**，避免不确定状态下产生写操作。

### 2. 图片上传：`POST /api/zpupload/uploadSingle`

未命中预检，或显式跳过预检时，发送 `multipart/form-data`。四个 part 的顺序固定为：

```text
sp
sig
app_id
file
```

`file` part 的 Content-Type 为 `application/octet-stream`（不是按扩展名填写 `image/png` 等）。
`sp` 中包含 commons 及：

```text
fileType = "0"
multipartType = "0"
securityId = 上传上下文的 token
source = "chat_file"
```

实测响应为 gzip 压缩的 JSON，成功样本 `code=0`；`requests` 自动解 gzip，不要再按搜索接口的加密响应路径重复解码。
JSON 中的 `code`、`message`、`zpData` 属于服务端响应，`_upload` 是 bosscli 附加的本地元数据，不能混淆两者。

## 重试、重定向与验证边界

- **不自动重试上传**，避免请求可能已成功接收时再次提交。超时或连接断开不等于服务端一定未收到文件，需人工判断是否再次确认上传。
- **不跟随重定向**，避免把 `t2` 等会话信息发送到非预期目标。
- 协议依据是 2026-11-11 的 App 历史捕获；签名通过历史捕获离线重算吻合验证。
- 验证范围包括 mock 与 localhost HTTP 集成测试：校验字段、签名、编码、multipart、响应及安全边界。
  localhost 测试不等于访问 BOSS 服务器。
- 本轮执行结果：`pytest tests -q` **73 passed**；`pip check` 和 `git diff --check` 通过。
- 新代码与既有私有捕获的离线对照：两条请求的解密参数、账号密钥签名、正文位置均吻合；验证脚本未发出任何请求。LZ4 编码允许不同但可解出完全相同的参数。
- 后续用户明确选择并确认一个岗位、招聘者和图片后，完整 `outreach` 流程已真实完成：打招呼 code 0、图片上传 code 0、匹配的消息业务回执及 `sent` 记录。该验证为单次授权操作，不代表可以自动联系其他招聘者；独立 `upload` 仍不发消息。
