# WB2Api

[English](README.md) · **简体中文** · [繁體中文](README.zh-TW.md)

将 Wabi 接入 OpenAI 兼容客户端，提供账号与 API Key 的 Web 管理台。

## 主要功能

- 流式对话与提示词模拟 Function Calling。
- 图片生成，返回真实图片链接。
- 账号管理、激活状态检查、原地重验与凭据导出。
- 会话令牌按需刷新，账号凭据加密保存。
- 适配手机的管理页，支持浅色、暗色，默认跟随系统。
- 提供 AMD64 和 ARM64 Docker 镜像。

## 模型入口

| 模型 | 用途 | 状态 |
| --- | --- | --- |
| `wabi` | 对话与客户端工具调用 | 可用 |
| `wabi-image` | 图片生成 | 可用 |
| `wabi-video` | 视频生成 | 暂未启用，尚未验证生成链路 |

以上名称按能力区分，不代表可选择底层基座模型。Function Calling 通过提示词兼容，由客户端执行返回的工具调用。可传 `reasoning_effort`（`none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`），通过提示词引导思考强度，不代表底层 Token 预算。常见采样和长度参数为兼容客户端而接收，但不在上游生效；响应头 `X-WB2Api-Ignored-Parameters` 会列出。

## 快速部署

```bash
git clone https://github.com/Likhixang/WB2Api.git
cd WB2Api
cp .env.example .env
chmod 600 .env
```

在 `.env` 中设置：

- `WB2API_ADMIN_KEY`：管理页密钥，建议使用长随机值。
- `WB2API_API_KEY`：客户端密钥，与管理员密钥不同。
- `WB2API_BIND_ADDRESS`：本机访问或反代建议使用 `127.0.0.1`。
- `WB2API_PORT`：宿主机端口，例如 `8080`。

```bash
docker compose pull
docker compose up -d
```

打开配置的地址，解锁管理台并添加 Wabi 账号，按提示完成短信与邮箱／Google 验证。账号需要初始化时，请先在官方 App 完成设置。远程访问请使用 HTTPS。

## 客户端接入

Base URL 填写服务地址加 `/v1`，API Key 使用管理页创建的密钥。管理员密钥不能用于客户端调用。

```bash
export WB2API_URL=http://127.0.0.1:8080
read -rs -p 'API key: ' WB2API_KEY; printf '\n'
curl "$WB2API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $WB2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"wabi","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

图片请求向 `/v1/images/generations` 发送 `model: "wabi-image"` 和 `prompt`，目前每次生成一张并返回 URL。也可在聊天客户端选择 `wabi-image`，获取图片链接。

## 持久请求诊断日志

管理页顶部「实时日志」可查看最新 200 条快照和进行中请求，支持每 3 秒自动刷新、请求 ID 与错误筛选；关闭窗口或切到后台暂停刷新。

请求摘要以 JSONL 保存到已有数据卷中的 `/data/logs/requests.log`，滚动文件为 `requests.log.1` 至 `.3`。`WB2API_LOG_MAX_BYTES=20971520` 表示当前文件与备份的**总容量**上限（默认 20 MiB）；`WB2API_LOG_BACKUPS=3` 将其分成 4 个单文件不超过 5 MiB 的文件。容量设为 `0` 关闭磁盘日志。启用时每个文件至少需要 1024 字节，备份数支持 0–32。调低限制后，启动时清理超大或超出数量的旧文件；最旧记录会淘汰，不会无限归档。

响应返回服务端生成的 `X-Request-ID` 便于定位。日志仅含 UTC 时间、请求 ID、白名单方法与端点、HTTP 状态、总耗时／首个响应体字节耗时（毫秒）、完成／断连标志和固定错误类别。流式首字节包含心跳，不等于模型首 Token 耗时。HTTP 200 不代表流完整成功，需检查 `completed` 与 `error_type`。不记录请求头、查询参数、请求／响应正文、凭据、路径内原始标识符或异常消息；未知路径记为 `<unmatched>`。目录／文件权限为 0700／0600。每个数据目录仅支持单进程／单 worker，不支持多进程共同滚动；磁盘写入失败不影响响应。容量限制仅针对这些请求日志，不包含数据库及 Docker／Uvicorn 控制台日志；容器日志滚动需另行配置。

请妥善保管 `.env` 和导出的凭据，并备份包含加密密钥的数据卷。仅使用有权访问的账号，遵守 Wabi 的条款与额度限制。
