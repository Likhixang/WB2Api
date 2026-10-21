# WB2Api 实施计划

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 建立私有 Wabi / OpenAI API 适配服务，附带 Web 管理页以及 GitHub Actions 双架构镜像。

**Architecture:** FastAPI 提供同源管理 API 和 OpenAI 路由；SQLite 保存账户、Fernet 加密令牌和持久化设备 ID；每个请求先建立 SSE 再提交消息，默认新建独立 Thread，避免不同客户端串话。原始物证缺少 SSE DTO，因此事件映射必须显式配置，不将推测协议冒充已确认协议。

**Tech Stack:** Python 3.12、FastAPI、httpx、cryptography、原生 HTML/CSS/JS、SQLite、Docker Compose、GHCR、GitHub Actions。

---

## 1. 上游与凭证管理
- 创建 `wb2api/store.py`：SQLite 账户持久化、令牌加密、非敏感账户列表。
- 创建 `wb2api/wabi.py`：固定 Android 头部、Firebase 登录与刷新、PKCE、SSE 解析与显式字段映射。
- 只使用物证已证实的端点；缺失映射明确报 `protocol_not_configured`。
- 默认逐账号串行、独立 Thread，无自动重放消息 POST。

## 2. API 与 Web
- 创建 `wb2api/main.py`：健康检查、管理鉴权、账户导入/启停、手机号登录、协议配置、模型列表、流式与非流式 chat completions。
- 创建 `wb2api/static/`：中文管理台，显示真实账户与准备状态、管理配置、导入刷新令牌、登录、API 使用方式。
- 管理 Key 仅保存在页面内存中；不返回令牌、不记录请求正文；不开放 CORS。

## 3. 部署与 CI
- 创建 `Dockerfile`、`docker-compose.yml`、`.github/workflows/ci.yml`。
- Compose 的绑定地址与宿主机端口要求部署者显式填写，网络由 Compose 自动创建，不携带开发环境约定；镜像使用 `latest`，应用只启一个 worker。
- GitHub Actions 集中跑必要的单元/API 测试、镜像构建及容器健康 smoke test，发布含 `linux/amd64`、`linux/arm64` 的 manifest；本地不构建、不运行测试。

## 4. 交付
- README 明确配置、GHCR 私有镜像登录、启动方法以及未完成登录后联调的边界。
- 通过 GitHub API 读回仓库私有性、CI 状态和 manifest 架构后再报告成功。
- 不新建 PR，不部署运行中的正式服务，不改动已有容器。
