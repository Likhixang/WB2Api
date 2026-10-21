# WB2Api

[English](README.md) · [简体中文](README.zh-CN.md) · **繁體中文**

將 Wabi 接入 OpenAI 相容用戶端，提供帳號與 API Key 的 Web 管理介面。

## 主要功能

- 串流對話與提示詞模擬 Function Calling。
- 圖片生成，回傳實際圖片連結。
- 帳號管理、啟用狀態檢查、原地重新驗證與憑證匯出。
- 工作階段權杖按需更新，帳號憑證加密儲存。
- 適合手機使用的管理頁，支援淺色、深色，預設跟隨系統。
- 提供 AMD64 和 ARM64 Docker 映像。

## 模型入口

| 模型 | 用途 | 狀態 |
| --- | --- | --- |
| `wabi` | 對話與用戶端工具呼叫 | 可用 |
| `wabi-image` | 圖片生成 | 可用 |
| `wabi-video` | 影片生成 | 尚未啟用，生成流程尚未驗證 |

以上名稱依能力區分，不代表能選擇底層基礎模型。Function Calling 透過提示詞相容，由用戶端執行回傳的工具呼叫。可傳入 `reasoning_effort`（`none`、`minimal`、`low`、`medium`、`high`、`xhigh`），透過提示詞引導思考強度，不代表底層 Token 預算。

## 快速部署

```bash
git clone https://github.com/Likhixang/WB2Api.git
cd WB2Api
cp .env.example .env
chmod 600 .env
```

在 `.env` 中設定：

- `WB2API_ADMIN_KEY`：管理頁金鑰，建議使用長隨機值。
- `WB2API_API_KEY`：用戶端金鑰，與管理員金鑰不同。
- `WB2API_BIND_ADDRESS`：本機存取或反向代理建議使用 `127.0.0.1`。
- `WB2API_PORT`：主機連接埠，例如 `8080`。

```bash
docker compose pull
docker compose up -d
```

開啟設定的位址，解鎖管理介面並新增 Wabi 帳號，依提示完成簡訊與信箱／Google 驗證。帳號需要初始化時，請先在官方 App 完成設定。遠端存取請使用 HTTPS。

## 用戶端連線

Base URL 填入服務位址加 `/v1`，API Key 使用管理頁建立的金鑰。管理員金鑰不能用於用戶端呼叫。

```bash
export WB2API_URL=http://127.0.0.1:8080
read -rs -p 'API key: ' WB2API_KEY; printf '\n'
curl "$WB2API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $WB2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"wabi","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

圖片請求向 `/v1/images/generations` 傳送 `model: "wabi-image"` 和 `prompt`，目前每次生成一張並回傳 URL。也可在聊天用戶端選擇 `wabi-image`，取得圖片連結。

請妥善保管 `.env` 和匯出的憑證，並備份包含加密金鑰的資料卷。僅使用有權存取的帳號，遵守 Wabi 的條款與用量限制。
