# WB2Api

**English** · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md)

Connect Wabi to API clients through OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages adapters, with a web console for accounts and API keys.

## Features

- Streaming text and client-executed tools across all three protocols.
- Image and video generation with URL output.
- Account management, activation checks, reauthentication, and credential export.
- On-demand session refresh and encrypted credential storage.
- Responsive console with light, dark, and system themes; AMD64 and ARM64 Docker images.

## Models and compatibility

| Model alias | Capability |
| --- | --- |
| `gpt-6.1-sol` | Chat and client-executed tools |
| `gpt-image-2.5-flare` | Image generation |
| `wan-2.1` | Video generation |

These are routing aliases, not proof of the upstream foundation model or a way to select it. Legacy aliases `wabi`, `wabi-image`, and `wabi-video` remain accepted; `/v1/models` lists only the three new aliases.

The three protocol adapters support text, streaming, and client tool-result continuations. Tools are prompt-based: the client executes calls and sends results back. This is not full native OpenAI/Anthropic feature parity. `reasoning_effort` (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) only guides prompts; it does not set a native reasoning budget or return real `reasoning_content`. Sampling parameters are accepted but not applied upstream; `X-WB2Api-Ignored-Parameters` identifies them.

Chat gateway budgets follow the [public Sol sizes](https://developers.openai.com/api/docs/models/gpt-6.1-sol): 1,050,000 total, 922,000 input, and 128,000 output tokens. Counts use **approximate `o200k_base`**, not a verified upstream tokenizer or billing usage. The full compiled prompt (history, tools, and gateway instructions) is counted. `max_completion_tokens` / `max_tokens` and Responses `max_output_tokens` set a smaller local output budget; completion tokens take precedence. Visible text stops with `length` / `incomplete` / `max_tokens`; oversized tool envelopes fail without emitting partial calls. These limits do not establish upstream model capacity or control its hidden reasoning. Chat requests use a 64 MiB transport guard instead of the former 200,000-character / 1 MiB gate. Responses continuation caches up to 32 MiB per entry, 64 MiB total, for 15 minutes; IDs may expire or be evicted. Media prompt limits remain unchanged.

`WB2API_DELETE_COMPLETED_THREADS=true` deletes only upstream threads created by the current request after successful completion (images/videos only after obtaining the artifact URL). Deletion is read back; failures are logged without discarding the response. Existing App conversations and incomplete/cancelled generations are not batch-deleted. Set it to `false` to keep new threads. Gateway deletion does not promise permanent CDN media retention.

## Quick start

```bash
git clone https://github.com/Likhixang/WB2Api.git
cd WB2Api
cp .env.example .env
chmod 600 .env
```

Set distinct strong `WB2API_ADMIN_KEY` and `WB2API_API_KEY` values in `.env`. Set `WB2API_BIND_ADDRESS` (`127.0.0.1` for local access or a reverse proxy) and `WB2API_PORT` (for example `8080`).

```bash
docker compose pull
docker compose up -d
```

Open the configured address, unlock the console, and add your Wabi account. Follow SMS and email/Google verification prompts. Complete any initial account setup in the official Wabi app. Use HTTPS for remote access.

## Connect a client

Use your server address plus `/v1` as the OpenAI Base URL, and a client API key from the console—not the admin key. Configure Anthropic clients to use the server's `/v1/messages` endpoint.

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

## Images and videos

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

Both generation endpoints return JSON with `created` and `data[].url`. Images currently support one image per request and URL output. `/v1/videos/generations` is a WB2Api JSON extension, **not** the official OpenAI Videos jobs API. In chat clients, select the image or video alias to receive a generated media link.

Video uses the independent `WABI_VIDEO_TIMEOUT=900` budget (seconds), not `WABI_REQUEST_TIMEOUT=120`. Long non-streaming requests can hit proxy timeouts such as HTTP 524; prefer `/v1/chat/completions` with `model: "wan-2.1"` and `stream: true`. Chat streaming sends heartbeats every 10 seconds while waiting. Configure client and proxy timeouts for long generations as well.

Keep `.env` and exported credentials private. Back up the data volume, including its encryption key. Use only authorized accounts and follow Wabi's terms and usage limits.
