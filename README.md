# WB2Api

Wabi → OpenAI-compatible API，附带中文 Web 管理页。镜像由 GitHub Actions 构建，支持 `linux/amd64` 与 `linux/arm64`。

> **首版状态**：管理、凭证保存、Firebase 刷新、API 转换和部署链路已实现。Wabi 登录后的 SSE 真实事件结构仍缺失；必须先根据自己的真实事件配置映射，聊天接口才会启用。没有有效账户的线上对话联调，不能将 CI 的 fixture 测试理解为 Wabi 聊天成功。

## 功能

- `/v1/models` 和 `/v1/chat/completions`，流式 SSE 与非流式响应。
- Web 管理台：账户导入、启停、手动连通性检查、短信登录、事件协议配置。
- Firebase Custom Token 兑换、Refresh Token 按需续期，账户凭证 Fernet 加密保存。
- 账户轮询分配，每账户串行聊天，设备 UUID 持久化。
- 先连接 SSE 再提交消息，避免提交和监听之间的事件竞态。
- 默认每次调用新建 Thread，避免将不同客户端仅凭 `user` 字段合并会话。
- 两个独立 Bearer Key：管理与推理分离，不返回账户令牌。
- 默认不记录 HTTP access log，不记录聊天正文或上游 token。

## 部署

### 1. 拉取私有仓库与镜像

```bash
gh repo clone Likhixang/WB2Api
cd WB2Api
```

GHCR 的私有包需要具有 `read:packages` 权限的 token。通过 stdin 登录，不要把 token 写入 Compose 或提交到仓库：

```bash
read -rsp 'GHCR token: ' GHCR_TOKEN; printf '\n'
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u Likhixang --password-stdin
unset GHCR_TOKEN
```

镜像：`ghcr.io/likhixang/wb2api:latest`。Docker 会自动选择宿主机对应架构。CI 还推送 `sha-<完整 commit SHA>` 供定位，不需要在正常部署中固定版本。

### 2. 设置服务 Key

```bash
cp .env.example .env
chmod 600 .env
python3 -c 'import secrets; print("WB2API_ADMIN_KEY=" + secrets.token_urlsafe(32)); print("WB2API_API_KEY=" + secrets.token_urlsafe(32))'
```

将生成的两个不同 Key 填入本地 `.env`。每个至少 24 字符；`.env` 被 Git 忽略。不要将账号 token 填进 Compose 文件。

另外显式填写 `WB2API_BIND_ADDRESS`（监听地址）、`WB2API_PORT`（宿主机端口）。网络由 Compose 自动创建，无需预先建立特定网络。切勿未经保护直接暴露到公网。

### 3. 启动

填好 `.env` 后即可启动，无需预建 Docker 网络：

```bash
docker compose pull
docker compose up -d
```

管理页地址由你填写的监听地址与宿主机端口决定。本仓库不关联任何个人服务器、反代域名或网络名称。远程访问应使用 SSH 隧道或 HTTPS 反代；SSE 需关闭代理缓冲，并将读取超时设为大于 `WABI_REQUEST_TIMEOUT`。

容器使用非 root UID `10001`，只读根文件系统、无 Linux capabilities、只启一个 worker。数据放在 `wb2api_data` volume。**数据库与 `encryption.key` 必须一并备份**；丢失 key 无法恢复已保存的凭证。加密不能防御同时取得数据库与加密 key 的主机管理员。

## 首次配置

1. 使用 `WB2API_ADMIN_KEY` 解锁管理页。页面只在内存中保存 Key，刷新页面后需重新输入。
2. 导入你本人账户的 Firebase Refresh Token 与对应 Firebase API Key；也可使用手机号验证码登录。Firebase API Key 是项目标识，不是账号凭证；本项目不内置猜测值。
3. 如果登录要求 Google 或邮箱附加验证，首版会明确提示，**不尝试绕过**；请在官方 App 完成后导入自己的有效凭证。
4. 依据自己的真实 Wabi SSE 事件设置事件名和 JSON 字段路径。默认事件名数组为空，聊天请求返回 `501 protocol_not_configured`。
5. 点击账户检查可手动请求 `/api/v1/me`。不要把凭证存在管理页的文本说明、日志或仓库中。

### 协议配置

```json
{
  "event_type_path": "type",
  "turn_id_path": "turn_id",
  "thread_id_path": "thread_id",
  "text_path": "delta.text",
  "delta_events": [],
  "done_events": [],
  "error_events": [],
  "mode": "delta"
}
```

**以上路径是可编辑的结构起点，并非已证实的 Wabi SSE 字段。** `delta_events` / `done_events` 必须填入真实事件名；若 JSON 没有事件类型，则使用 SSE 的 `event:` 名称。点号路径只遍历 JSON 对象。只处理匹配目标 turn / thread 的事件；未知事件忽略，无关联 ID 的事件不会作为聊天输出。

`mode=delta` 表示文本追加，`mode=snapshot` 表示递增全文快照。遇到非前缀替换应显式报错，不能悄悄拼接错误内容。没有自动断线重放；流中断会报错，避免重复提交收费消息。

## API 使用

Base URL：你的服务地址加 `/v1`；Key：`WB2API_API_KEY`；模型：`wabi`。

```bash
curl "$WB2API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $WB2API_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"wabi","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### 兼容性边界

- `wabi` 是上游默认助手的别名，**不代表可选择底层供应商模型**。
- 首版支持文本 `system`、`developer`、`user`、`assistant` 消息。多轮历史以带角色标签的文本送入新 Thread，不保证与原生 role / system 优先级一致。
- 不支持工具调用、图片、文件、音频、JSON schema、temperature 或 max_tokens 等未证实的上游控制参数。额外参数明确返回 `422`，不静默假装支持。
- 不伪造 usage、token 数、模型名称；无可靠上游用量时不输出 `usage`。
- `user` 仅作为兼容字段接收，不用于跨用户共享 Thread。
- 流建立前的错误返回标准 HTTP JSON；开始输出后的错误发送 SSE `error` 对象，**不伪造成功结束标志**。
- 短信发送必须显式点击；服务不会在启动时自动登录或发送短信。
- 仅用于你有权使用的账户，遵守 Wabi 服务条款和额度限制。

## 配置变量

- `WB2API_ADMIN_KEY`：必需，与 API Key 不同。
- `WB2API_API_KEY`：必需，至少 24 字符。
- `WB2API_DATA_DIR`：默认 `/data`。
- `WB2API_ENCRYPTION_KEY`：可选 Fernet key；不设置时自动生成并保存在数据 volume 内。只能通过安全环境注入，改变前先备份现有数据。
- `WABI_BASE_URL`：默认 `https://app.getwabi.ai`，仅管理员环境配置，Web 不可任意修改。
- `WABI_REQUEST_TIMEOUT`：单次上游操作的超时预算，默认 120 秒。
- `WABI_TIME_ZONE`：默认 `Asia/Shanghai`。

## CI 与开发

所有构建与测试在 `.github/workflows/ci.yml` 中执行：

1. 安装依赖，集中运行 unit / API fixture 测试及 Compose 语法检查。
2. 使用 Buildx 构建并发布 `linux/amd64,linux/arm64` 镜像。
3. 检查 manifest 架构，启动已发布的 AMD64 镜像完成健康检查、管理鉴权 smoke test。

没有本地构建要求，不需要 Node/npm；管理页以原生 HTML/CSS/JS 随 Python 包直接发布。CI 不携带真实 Wabi 账户，不执行短信发送或聊天请求。ARM64 会进行镜像构建验证，但不等同于在真实 ARM 宿主机完成聊天联调。

## 依据与限制

协议来自 Wabi Android `2.0.2` / build `1010` 的静态物证与少量未登录探针。原报告未执行登录后聊天、未证明服务端无风控；缺失 split/native 的样本也不能视为完整原生代码审计。首版不依赖任何声称可绕过认证的手段。
