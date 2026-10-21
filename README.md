# WB2Api

**English** · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md)

Use Wabi through an OpenAI-compatible API, with a web console for accounts and API keys.

## Features

- Chat with streaming responses and prompt-based Function Calling.
- Generate images and return image URLs.
- Manage accounts, check activation status, reauthenticate, and export credentials.
- Refresh session tokens on demand and keep stored credentials encrypted.
- Responsive web console with light, dark, and system themes. Follows your system by default.
- Docker images for AMD64 and ARM64.

## Models

| Model | Capability | Status |
| --- | --- | --- |
| `wabi` | Chat and client-executed tools | Available |
| `wabi-image` | Image generation | Available |
| `wabi-video` | Video generation | Not enabled; generation has not been verified |

These are capability aliases, not selectable foundation models. Function Calling uses prompts rather than a native Wabi API; the client executes the returned calls. There is no verified reasoning-effort control.

## Quick start

```bash
git clone https://github.com/Likhixang/WB2Api.git
cd WB2Api
cp .env.example .env
chmod 600 .env
```

Set these values in `.env`:

- `WB2API_ADMIN_KEY`: a strong key for the web console.
- `WB2API_API_KEY`: a different key for API clients.
- `WB2API_BIND_ADDRESS`: use `127.0.0.1` for local access or a reverse proxy.
- `WB2API_PORT`: the host port, for example `8080`.

```bash
docker compose pull
docker compose up -d
```

Open the configured address, unlock the console, and add your Wabi account. Follow the SMS and email/Google verification prompts. If the account needs initial setup, complete it in the official Wabi app. Use HTTPS for remote access.

## Connect a client

Set the Base URL to your server address followed by `/v1`, then use an API key from the console. The admin key is not a client API key.

```bash
export WB2API_URL=http://127.0.0.1:8080
read -rs -p 'API key: ' WB2API_KEY; printf '\n'
curl "$WB2API_URL/v1/chat/completions" \
  -H "Authorization: Bearer $WB2API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"wabi","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

For images, send `model: "wabi-image"` and `prompt` to `/v1/images/generations`. Currently supports one image per request and URL output. You can also select `wabi-image` in a chat client to receive an image link.

Keep `.env` and exported credentials private. Back up the data volume, including its encryption key. Use only accounts you are authorized to access and follow Wabi's terms and usage limits.
