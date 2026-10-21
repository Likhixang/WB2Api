# WB2Api

[English](README.md) · **简体中文** · [繁體中文](README.zh-TW.md)

通过 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 适配接口连接 Wabi，提供账号与 API Key 的 Web 管理台。

## 主要功能

- 三种协议支持流式文本与客户端工具调用。
- 图片与视频生成，返回媒体 URL。
- 账号管理、激活检查、原地重验与凭据导出。
- 会话令牌按需刷新，账号凭据加密保存。
- 适配手机的管理页，支持浅色、暗色与跟随系统；提供 AMD64 和 ARM64 Docker 镜像。

## 模型别名与兼容范围

| 模型别名 | 用途 |
| --- | --- |
| `gpt-6.1-sol` | 对话与客户端工具调用 |
| `gpt-image-2.5-flare` | 图片生成 |
| `wan-2.1` | 视频生成 |

以上是路由别名，不证明上游实际基座型号，也不代表可选择底层模型。旧别名 `wabi`、`wabi-image`、`wabi-video` 继续兼容；`/v1/models` 仅列出三个新别名。

三种协议适配支持文本、流式及客户端工具结果续接。工具调用通过提示词兼容，由客户端执行并回传结果，不代表完整原生 OpenAI／Anthropic 功能。`reasoning_effort`（`none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`）仅引导提示词，不设置原生推理预算，也不返回真实 `reasoning_content`。采样参数虽被接受，但不在上游生效；响应头 `X-WB2Api-Ignored-Parameters` 会列出。

对话网关预算按 [Sol 官方规格](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 设置：总上下文 1,050,000、输入 922,000、输出 128,000 Tokens。使用 **`o200k_base` 近似计数**，不代表上游真实 tokenizer、用量或已验证容量。计数涵盖完整编译提示词（历史、工具和网关指令）。`max_completion_tokens`／`max_tokens` 及 Responses 的 `max_output_tokens` 可指定更小的本地输出预算，前者优先；普通文本达到预算后以 `length`／`incomplete`／`max_tokens` 终止，工具信封超限则明确报错，不发送半截调用。预算不控制上游隐藏推理。对话不再有旧的 20 万字符／1 MiB 限制，传输保护为 64 MiB。Responses 续传缓存每项最多 32 MiB、总计 64 MiB、保留 15 分钟，可能过期或被淘汰。媒体提示词限制不变。

`WB2API_DELETE_COMPLETED_THREADS=true` 默认在成功完成后删除本次请求新建的上游会话，图片和视频须先取得实际附件 URL；删除后读回确认，失败只记日志、不丢弃结果。不批量删除 App 已有会话，未完成或中途取消的任务不贸然删除。设为 `false` 可保留新会话；删除会话不代表 CDN 媒体永久保存。

## 快速部署

```bash
git clone https://github.com/Likhixang/WB2Api.git
cd WB2Api
cp .env.example .env
chmod 600 .env
```

在 `.env` 中设置不同的高强度 `WB2API_ADMIN_KEY` 与 `WB2API_API_KEY`，并设置 `WB2API_BIND_ADDRESS`（本机访问或反代使用 `127.0.0.1`）和 `WB2API_PORT`（例如 `8080`）。

```bash
docker compose pull
docker compose up -d
```

打开配置的地址，解锁管理台并添加 Wabi 账号，按提示完成短信与邮箱／Google 验证。账号需要初始化时，请先在官方 App 完成设置。远程访问请使用 HTTPS。

## 客户端接入

OpenAI 客户端的 Base URL 填服务地址加 `/v1`，API Key 使用管理页创建的客户端密钥，而非管理员密钥。Anthropic 客户端应连接到该服务的 `/v1/messages` 端点。

```bash
export WB2API_URL=http://127.0.0.1:8080
read -rs -p 'API key: ' WB2API_KEY; printf '\n'

# OpenAI Chat Completions
curl -N "$WB2API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $WB2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol","messages":[{"role":"user","content":"Hello"}],"stream":true}'

# OpenAI Responses
curl -N "$WB2API_URL/v1/responses" \
  -H "Authorization: Bearer $WB2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol","input":"Hello","stream":true}'

# Anthropic Messages
curl -N "$WB2API_URL/v1/messages" \
  -H "x-api-key: $WB2API_KEY" \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol","max_tokens":1024,"messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

## 图片与视频

```bash
curl "$WB2API_URL/v1/images/generations" \
  -H "Authorization: Bearer $WB2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-image-2.5-flare","prompt":"A mountain lake at sunrise"}'

curl "$WB2API_URL/v1/videos/generations" \
  -H "Authorization: Bearer $WB2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"wan-2.1","prompt":"Clouds drifting over a mountain lake"}'
```

两个生成接口均返回含 `created` 与 `data[].url` 的 JSON。图片目前每次生成一张，仅返回 URL。`/v1/videos/generations` 是 WB2Api 的 JSON 扩展接口，**不是** OpenAI 官方 Videos 任务 API。聊天客户端也可选择图片或视频别名，获取生成的媒体链接。

视频使用独立的 `WABI_VIDEO_TIMEOUT=900` 超时预算（秒），不受 `WABI_REQUEST_TIMEOUT=120` 控制。非流式长请求可能触发代理超时（如 HTTP 524）；建议通过 `/v1/chat/completions` 使用 `model: "wan-2.1"` 和 `stream: true`，等待期间每 10 秒发送心跳。客户端和代理的超时也需适当延长。

请妥善保管 `.env` 和导出的凭据，并备份包含加密密钥的数据卷。仅使用有权访问的账号，遵守 Wabi 的条款与额度限制。
