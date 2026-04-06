FROM ubuntu:22.04

# Avoid prompts from apt
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && apt-get install -y \
    software-properties-common \
    && add-apt-repository ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    python3.11 \
    python3.11-venv \
    python3.11-dev \
    python3-pip \
    git \
    curl \
    bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# Set python3.11 as default python3 and python
RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

WORKDIR /app

# Ensure pip is for 3.11
RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11

# Copy project files and install cc-mini in editable mode
COPY . .
RUN pip install -e ".[dev]"

CMD ["/bin/bash"]
