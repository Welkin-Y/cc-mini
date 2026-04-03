# Using LM Studio (LMS) Backend

`cc-mini` supports using [LM Studio](https://lmstudio.ai/) as a local LLM backend. This allows you to run models locally on your machine while maintaining compatibility with the OpenAI-style wire format.

## Prerequisites

- **LM Studio**: Installed and running on your machine.
- **OpenAI Python Package**: The `openai` package must be installed in your environment.
  ```bash
  pip install openai
  ```

## LM Studio Setup

1. Open LM Studio.
2. Download and load your desired model.
3. Go to the **Local Server** tab (the double-arrow icon on the left).
4. Click **Start Server**.
5. Note the "Server Base URL" (usually `http://localhost:1234`).

## Configuration

You can configure `cc-mini` to use LM Studio via environment variables, a configuration file, or CLI arguments.

**Note for Docker users:** If you are running `cc-mini` in a container, you must change `localhost` to `host.docker.internal` (or your host's IP) in the base URL to reach the LM Studio server.

### Option 1: Environment Variables

Set the following environment variables in your shell:

```bash
export CC_MINI_PROVIDER=lmstudio
# Optional: defaults to http://localhost:1234/v1
export LMSTUDIO_BASE_URL=http://localhost:1234/v1
# Optional: defaults to "lm-studio"
export LMSTUDIO_API_KEY=lm-studio
```

### Option 2: Configuration File

Create or edit your `config.toml` (or use the `--config` flag) with the following section:

```toml
provider = "lmstudio"

[lmstudio]
base_url = "http://localhost:1234/v1"
api_key = "lm-studio"
```

### Option 3: CLI Arguments

Pass the provider and optional parameters directly when running `cc-mini`:

```bash
cc-mini --provider lmstudio --model "local-model" --base-url "http://localhost:1234/v1"
```

### Option 4: Docker

When running `cc-mini` inside a Docker container, use `host.docker.internal` to access the LM Studio server running on your host machine.

- **macOS/Windows (Docker Desktop):** Use `host.docker.internal` as the hostname.
- **Linux:** Ensure your `docker-compose.yml` or `docker run` command handles the `host.docker.internal` mapping (usually via `extra_hosts` or `--add-host`).

The project provides a `Makefile` for convenience. Update your `.env` file or `docker-compose.yml` with the following:

```yaml
services:
  cc-mini:
    environment:
      - CC_MINI_PROVIDER=lmstudio
      - CC_MINI_MODEL=local-model
      - LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1
      - LMSTUDIO_API_KEY=lm-studio
```

Then run:

```bash
# Build the image (required if Dockerfile changed)
make build

# Run the interactive REPL
make run

# Or open a bash session to run cc-mini manually
make bash
cc-mini
```

If you prefer `docker run`:

```bash
# Run on macOS/Windows
docker run -it --rm \
  -v "$(pwd):/app" \
  -e CC_MINI_PROVIDER=lmstudio \
  -e CC_MINI_MODEL=local-model \
  -e LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1 \
  cc-mini cc-mini
```

## Usage Notes

- **Model Name**: By default, `cc-mini` uses `local-model` as the model name. If you have multiple models loaded or want to be specific, use the `--model` flag with the model ID shown in LM Studio.
- **Tools support**: Local models vary in their ability to handle tool-use (function calling). For best results, use models that are specifically fine-tuned for tool-use (e.g., Hermes-3, Llama-3-Groq-Tool-Use).
- **Performance**: Speed depends entirely on your local hardware (GPU/CPU) and the size of the model you are running.
- **Companion/Buddy**: Side features like the "Buddy" companion will also use the LM Studio backend if it is selected as the primary provider.
