FROM python:3.14.2-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iptables \
        procps \
        python3-apt \
        ufw \
    && python -m pip install --no-cache-dir ansible-core==2.21.2 \
    && ansible-galaxy collection install community.general:==13.1.0 ansible.posix:==2.2.0 \
    && rm -rf /var/lib/apt/lists/*
