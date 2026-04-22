# Using LM Studio (LMS) Backend

`cc-mini` supports using [LM Studio](https://lmstudio.ai/) as a local LLM backend. This keeps `cc-mini` on an OpenAI-compatible wire format while the model runs on your machine.

## LM Studio Setup

1. Open LM Studio.
2. Download and load the model you want.
3. Open the **Local Server** tab.
4. Click **Start Server**.
5. Note the server base URL, usually `http://localhost:1234`.

## Local Shell Configuration

```bash
export CC_MINI_PROVIDER=lmstudio
export CC_MINI_MODEL=local-model
export LMSTUDIO_BASE_URL=http://localhost:1234/v1
export LMSTUDIO_API_KEY=lm-studio
```

## Docker Configuration

When `cc-mini` runs in Docker and LM Studio runs on your Windows host, use `host.docker.internal` so the container can reach the host service.

The included `docker-compose.yml` already defaults to:

```bash
export CC_MINI_PROVIDER=lmstudio
export CC_MINI_MODEL=local-model
export LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1
export LMSTUDIO_API_KEY=lm-studio
```

Then:

```bash
make build
make run
```

For a shell inside the container:

```bash
make bash
```

If your LM Studio server is reachable through a Windows LAN IP instead of Docker Desktop host routing, override `LMSTUDIO_BASE_URL` before `make run`.
