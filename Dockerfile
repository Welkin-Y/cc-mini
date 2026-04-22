FROM python:3.9-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update -o Acquire::Retries=3 && apt-get install -y --fix-missing --no-install-recommends \
    bash \
    bubblewrap \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY src ./src
COPY tests ./tests
COPY docs ./docs
COPY assets ./assets
COPY install.sh ./install.sh
COPY notebooks ./notebooks

RUN pip install -e ".[dev,notebook]"

CMD ["cc-mini"]
