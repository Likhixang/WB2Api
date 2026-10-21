# WB2Api

[English](README.md) · [简体中文](README.zh-CN.md) · **繁體中文**

透過 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages 配接介面連接 Wabi，提供帳號與 API Key 的 Web 管理介面。

## 主要功能

- 三種協定支援串流文字與用戶端工具呼叫。
- 圖片與影片生成，回傳媒體 URL。
- 帳號管理、啟用檢查、原地重新驗證與憑證匯出。
- 工作階段權杖按需更新，帳號憑證加密儲存。
- 適合手機使用的管理頁，支援淺色、深色與跟隨系統；提供 AMD64 和 ARM64 Docker 映像。

## 模型別名與相容範圍

| 模型別名 | 用途 |
| --- | --- |
| `gpt-6.1-sol` | 對話與用戶端工具呼叫 |
| `gpt-image-2.5-flare` | 圖片生成 |
| `wan-2.1` | 影片生成 |

以上是路由別名，不證明上游實際基礎型號，也不代表能選擇底層模型。舊別名 `wabi`、`wabi-image`、`wabi-video` 繼續相容；`/v1/models` 僅列出三個新別名。

三種協定配接支援文字、串流及用戶端工具結果續接。工具呼叫透過提示詞相容，由用戶端執行並回傳結果，不代表完整原生 OpenAI／Anthropic 功能。`reasoning_effort`（`none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`）僅引導提示詞，不設定原生推理預算，也不回傳真實 `reasoning_content`。取樣參數雖被接受，但不在上游生效；回應標頭 `X-WB2Api-Ignored-Parameters` 會列出。

對話閘道預算依 [Sol 官方規格](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 設定：總上下文 1,050,000、輸入 922,000、輸出 128,000 Tokens。使用 **`o200k_base` 近似計數**，不代表上游真實 tokenizer、用量或已驗證容量。計數包含完整編譯提示詞（歷史、工具與閘道指令）。`max_completion_tokens`／`max_tokens` 及 Responses 的 `max_output_tokens` 可指定較小的本地輸出預算，前者優先；一般文字達到預算時以 `length`／`incomplete`／`max_tokens` 終止，工具信封超限則明確報錯，不傳送不完整呼叫。預算不控制上游隱藏推理。對話不再有舊的 20 萬字元／1 MiB 限制，傳輸保護為 64 MiB。Responses 續傳快取每項最多 32 MiB、合計 64 MiB、保留 15 分鐘，可能過期或被淘汰。媒體提示詞限制不變。

`WB2API_DELETE_COMPLETED_THREADS=true` 預設在成功完成後刪除本次請求建立的上游會話，圖片和影片須先取得實際附件 URL；刪除後讀回確認，失敗僅記錄日誌、不丟棄結果。不批次刪除 App 既有會話，未完成或中途取消的工作不貿然刪除。設為 `false` 可保留新會話；刪除會話不代表 CDN 媒體永久保存。

## 快速部署

```bash
git clone https://github.com/Likhixang/WB2Api.git
cd WB2Api
cp .env.example .env
chmod 600 .env
```

在 `.env` 中設定不同的高強度 `WB2API_ADMIN_KEY` 與 `WB2API_API_KEY`，並設定 `WB2API_BIND_ADDRESS`（本機存取或反向代理使用 `127.0.0.1`）和 `WB2API_PORT`（例如 `8080`）。

```bash
docker compose pull
docker compose up -d
```

開啟設定的位址，解鎖管理介面並新增 Wabi 帳號，依提示完成簡訊與信箱／Google 驗證。帳號需要初始化時，請先在官方 App 完成設定。遠端存取請使用 HTTPS。

## 用戶端連線

OpenAI 用戶端的 Base URL 填服務位址加 `/v1`，API Key 使用管理頁建立的用戶端金鑰，而非管理員金鑰。Anthropic 用戶端應連接到此服務的 `/v1/messages` 端點。

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

## 圖片與影片

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

兩個生成介面均回傳含 `created` 與 `data[].url` 的 JSON。圖片目前每次生成一張，僅回傳 URL。`/v1/videos/generations` 是 WB2Api 的 JSON 擴充介面，**不是** OpenAI 官方 Videos 工作 API。聊天用戶端也可選擇圖片或影片別名，取得生成的媒體連結。

影片使用獨立的 `WABI_VIDEO_TIMEOUT=900` 逾時預算（秒），不受 `WABI_REQUEST_TIMEOUT=120` 控制。非串流長請求可能觸發代理逾時（如 HTTP 524）；建議透過 `/v1/chat/completions` 使用 `model: "wan-2.1"` 和 `stream: true`，等待期間每 10 秒傳送心跳。用戶端和代理的逾時也需適當延長。

請妥善保管 `.env` 和匯出的憑證，並備份包含加密金鑰的資料卷。僅使用有權存取的帳號，遵守 Wabi 的條款與用量限制。
