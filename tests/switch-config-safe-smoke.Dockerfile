FROM ubuntu:26.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        coreutils \
        diffutils \
        iproute2 \
        jq \
        sed \
    && rm -rf /var/lib/apt/lists/*
