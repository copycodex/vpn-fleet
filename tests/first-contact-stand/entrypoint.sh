#!/usr/bin/env bash
set -euo pipefail

initial_password='Stand-Initial9!Password'
echo "fresh:${initial_password}" | chpasswd
rm -rf /home/fresh/.ssh
rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub
ssh-keygen -A >/dev/null
if [ "${EXPIRE_PASSWORD:-0}" = 1 ]; then
  passwd -e fresh >/dev/null
fi

if [ -n "${SLOW_PAM_SECONDS:-}" ]; then
  case "$SLOW_PAM_SECONDS" in
    *[!0-9]*|'') echo 'invalid SLOW_PAM_SECONDS' >&2; exit 2 ;;
  esac
  sed -i "/pam_pwquality[.]so/i password required pam_exec.so quiet /usr/bin/sleep $SLOW_PAM_SECONDS" \
    /etc/pam.d/common-password
fi

if [ -n "${STTY_MODE:-}" ]; then
  case "$STTY_MODE" in
    inlcr|-icrnl) ;;
    *) echo 'invalid STTY_MODE' >&2; exit 2 ;;
  esac
  cat > /usr/local/sbin/answer-stand-command <<'EOF'
#!/bin/sh
set -eu
if [ -t 0 ]; then
  stty ANSWER_STTY_MODE
fi
exec /bin/sh -c "${SSH_ORIGINAL_COMMAND:-}"
EOF
  sed -i "s/ANSWER_STTY_MODE/$STTY_MODE/" /usr/local/sbin/answer-stand-command
  chmod 0755 /usr/local/sbin/answer-stand-command
  printf 'ForceCommand /usr/local/sbin/answer-stand-command\n' \
    > /etc/ssh/sshd_config.d/91-answer-stand.conf
fi

cat > /etc/ssh/sshd_config.d/90-first-contact-stand.conf <<'EOF'
PasswordAuthentication yes
KbdInteractiveAuthentication no
UsePAM yes
PermitRootLogin no
PerSourcePenalties no
EOF

exec /usr/sbin/sshd -D -e
