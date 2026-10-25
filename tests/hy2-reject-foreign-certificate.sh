#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "usage: $0 <sing-box> <server-config> <client-config> <work-dir>" >&2
  exit 2
fi

singbox_bin="$1"
server_config="$2"
client_config="$3"
work_dir="$4"
client_log="$work_dir/foreign-certificate-client.log"
server_log="$work_dir/foreign-certificate-server.log"
http_log="$work_dir/foreign-certificate-http.log"

server_pid=""
client_pid=""
http_pid=""

cleanup() {
  for pid in "$client_pid" "$server_pid" "$http_pid"; do
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT

python3 -m http.server 29081 --bind 127.0.0.1 \
  >"$http_log" 2>&1 &
http_pid=$!

"$singbox_bin" run -c "$server_config" -D "$work_dir" \
  >"$server_log" 2>&1 &
server_pid=$!

"$singbox_bin" run -c "$client_config" -D "$work_dir" \
  >"$client_log" 2>&1 &
client_pid=$!

sleep 1

if curl --fail --silent --show-error --max-time 5 \
  --socks5-hostname 127.0.0.1:19080 http://127.0.0.1:29081/ \
  >/dev/null 2>&1; then
  echo "ERROR: Hy2 client accepted a server with a foreign certificate" >&2
  exit 1
fi

sleep 1
if ! grep -Eqi 'certificate|x509|public key' "$client_log"; then
  echo "ERROR: Hy2 connection failed without a certificate rejection" >&2
  sed -E 's/(password|uuid)[^ ]*/\1=<redacted>/g' "$client_log" >&2
  exit 1
fi

echo "hy2 foreign-certificate rejection: ok"
