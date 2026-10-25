FROM python:3.14.2-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        openssl \
        passwd \
        procps \
        unzip \
    && python -m pip install --no-cache-dir ansible-core==2.21.2 \
    && rm -rf /var/lib/apt/lists/*
