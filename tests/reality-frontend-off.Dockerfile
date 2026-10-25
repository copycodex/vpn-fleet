FROM python:3.14.2-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends haproxy python3-apt procps \
    && python -m pip install --no-cache-dir ansible-core==2.21.2 \
    && rm -rf /var/lib/apt/lists/*
