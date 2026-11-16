# 图片上传协议记录与旧原型纠正

更新：**2026-11-11**。此处记录已实测的 App 图片上传协议及 bosscli 的验证边界。
CLI 用法、Python API 和图片处理说明见[图片上传文档](image-upload.md)。

**功能仅上传并返回地址，不发送 IM 图片消息，也不打招呼。返回 `url` 不代表对方收到图片。**

## 已确认的请求形状

固定 host：`https://api-and.zhipin.com`；原始业务字段放入 `sp`，不放到 query。
两个上传端点都使用账号 `secretKey` 签名，`sig` 无 `strA`，`app_id` 签名后追加。
除签名密钥外，实际请求还需要有效的 `t2`、`client_info`、`uniqid` 等会话/设备参数。

| 步骤 | 请求 | 正文与字段 |
|---|---|---|
| MD5 秒传预检（默认启用） | `POST /api/zpupload/quicklyUpload` | urlencoded 正文仅 `sp`、`sig`、`app_id`；`sp` 含 commons、`source='chat_file'`、上传 `securityId`、`gid=''`、`fileMd5`、`fileSize`。 |
| 上传图片 | `POST /api/zpupload/uploadSingle` | multipart part 顺序为 `sp` / `sig` / `app_id` / `file`；`file` 的 Content-Type 为 `application/octet-stream`；`sp` 含 commons、`fileType='0'`、`multipartType='0'`、上传 `securityId`、`source='chat_file'`。 |

预检仅在成功响应的 `zpData.url` 与 `zpData.tinyUrl` **两者都非空**时认定 cache hit，直接返回地址，不再上传。
成功响应的 `zpData` 为空时继续 `uploadSingle`；不能仅凭 `code=0` 或一个地址就宣称秒传命中。
预检非零业务码、畸形响应或只有一个地址的 partial cache 均报错停止，不继续上传。
实测成功响应为 gzip JSON、`code=0`，由 `requests` 自动解 gzip。
文件接收后，服务端 metadata 可能与本次上传字节的 MD5/大小不同；不要把它当作原始文件的校验和。

## 2026-11-06 原型：已撤回的假设

旧版文档只是静态分析与离线组包的**原型记录**，并非实际上传链路验证；以下假设已纠正，不应继续作为调用依据：

- 不再以旧的 `naUploadImage` 或其它候选路由作为本功能的图片上传入口，改为已观测的 `quicklyUpload` → `uploadSingle`。
- 撤回“上传端点免账号密钥、可用 `key=None`”的假设：本协议必须使用账号 `secretKey`。
- 撤回“把原始字段和签名放进 query”的描述：预检是三字段 urlencoded 正文，上传是四 part multipart 正文。
- 移除猜测性的 `route`、`part`、`field`/`fields`、`key` 自定义接口。公开入口为 `BossClient.upload_image(path, security_id=None, *, precheck=True, original=False) -> dict`；`upload_file` 只作图片接口的兼容别名。
- 删除“只要有 `t2` 就能上传”“`code 7` 必定只是 token 问题”以及自动使用用户 session/真实简历做在线上传、随后发消息的承诺。

图片上传用的 `securityId` 必须来自合适的上传上下文：显式选项（`--security-id` 或 `--security-id-file`，互斥）优先，
否则读取 `session.upload_defaults.securityId`，不能随意拿职位搜索卡片的 `securityId` 替代。

## 接入 bosscli 的约束

- `bosscli [--session PATH] upload IMAGE` 默认 **dry-run、纯本地**，无 token 也能预览；只有 `--confirm` 才访问服务器。
- 仅支持 JPEG / PNG / WebP 单帧图片。默认 EXIF 转正、透明白底合成、RGB JPEG；`quality=90` 是本地策略，不宣称与 Android 上传字节相同。`--original` 保留原始字节。
- 20 MiB、40M 像素是本地保护，不是服务端阈值。MD5/大小针对实际准备上传的字节。
- `--skip-precheck` 可跳过默认预检；`--token` 仅覆盖 `t2`。`--json` 包含 `code` / `message` / `zpData` 与本地 `_upload` 元数据。
- 无自动重试上传、无跟随重定向，避免重复上传及 token 泄漏。超时不保证服务端尚未接收文件。
- 不硬编码真实凭据、不从抓包自动导入 session、不把真实凭据或抓包私密内容写入 fixtures；预处理 helper 的内部 `bytes` 不进入日志。

## 验证口径

- **已有协议证据**：2026-11-11 的 App 历史捕获及服务端响应，不是本次新 CLI 的在线执行结果。
- **离线签名验证**：使用历史捕获参数重新计算签名吻合，不把请求可组装等同于服务器已接收。
- **自动化验证**：增加 mock 与 localhost HTTP 集成测试，校验预检/命中、multipart 字段与顺序、签名、gzip 响应及安全边界。localhost HTTP 并非 BOSS 线上接口。
- **后续获授权的线上验证**：用户明确选择单个岗位、招聘者和图片后，纯 Python `outreach` 已实测打招呼 code 0、图片上传 code 0，并取得匹配的聊天业务回执；journal 为 `sent`。仅一次操作、没有自动重发；不得把它当作联系其他人的通用授权。

本次文档更新不要求继续抓包、连接 ADB 或访问网络；实际测试执行结果由实现任务报告。
