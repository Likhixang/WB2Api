# 协议证据与联调边界

## 已有依据

来源：Wabi Android `2.0.2` / `1010` 的反编译交接资料，样本 SHA-256：
`fd15cf7fbad34fc13241afc984a991fc51d522ec767a4bac8610a5136024396f`。

- 默认 Base URL：`https://app.getwabi.ai`，App 可以受 Remote Config `api_base_url_v2` 覆盖。本服务使用管理员环境变量指定，不请求未知远程地址。
- 手机号：`POST /api/v1/auth/phone/send-code` 与 `/check-code`。
- PKCE：64 字节随机数 → base64url 无填充 verifier → SHA256 → base64url challenge。
- Firebase Custom Token 兑换与 Refresh Token 流程。
- `/api/v1/me`、`/api/v1/threads`、`/api/v1/threads/{id}/messages` 与 `/api/v1/chat/events` 路径。
- 消息提交的 `client_id`、`blocks`、`attachment_ids`、`source`、`page_context`。
- 提交响应包含 `turn_id`、`disposition`、`message`，而不是生成文本。
- Android 固定请求头与持久化设备 UUID。

## 仍需实际账户确认

- SSE 的事件名、关联 ID 路径、文本路径、完成/错误事件。
- 创建 Thread 的线上响应实际包裹形式。
- Firebase 项目的实际 API Key 与对应有效账号凭证。
- 账号是否需 Google、邮箱等附加验证，以及后端风控、额度与 SSE 连接限制。
- 底层供应商模型以及任何模型选择/用量字段。没有证据则不声称支持。

## 安全结论限度

原始分析未登录账号、未发送短信或聊天，只有少量未登录/无效 token 探针。APK 为缺少 split/native 的 base 样本。未发现业务拦截器中的 Pinning / App Check / Native 签名，不等于已证实所有组件不存在这些机制，更不等于服务端无风控。

## 实现原则

- 未配置真实 SSE 映射时返回明确 `501`，不自动试猜事件名。
- 测试中的 `fixture_*` 数据只用于验证本地适配代码，绝不作为线上结果展示。
- 不自动重放消息 POST；网络失败有可能发生在上游已受理之后，客户端重试可能产生新 Thread 或重复请求。
- 上游创建的 Thread 不自动删除，首版没有未经确认的清理动作。
- 管理界面不返回上游 token，不持久化管理 Key 到浏览器。
