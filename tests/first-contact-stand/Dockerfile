FROM ubuntu:26.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      openssh-server libpam-pwquality cracklib-runtime \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash fresh \
    && mkdir -p /run/sshd

COPY entrypoint.sh /usr/local/sbin/first-contact-stand
RUN chmod 0755 /usr/local/sbin/first-contact-stand

EXPOSE 22
ENTRYPOINT ["/usr/local/sbin/first-contact-stand"]
