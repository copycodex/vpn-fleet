#!/usr/bin/env bash
# Fail if the tree carries someone's coordinates: a real endpoint address, key
# material or a client credential. The check is by SHAPE, not by a list of
# literals — a list of the addresses we do not want published would itself
# publish them.
#
# Usage: scripts/scan-coordinates.sh [path ...]   (default: all tracked files)
set -euo pipefail
export LC_ALL=C

cd "$(dirname "${BASH_SOURCE[0]}")/.."

LIST="$(mktemp)"
REPORT="$(mktemp)"
trap 'rm -f "$LIST" "$REPORT" "$REPORT.filtered" "$REPORT.error"' EXIT

if [ "$#" -gt 0 ]; then
  printf '%s\0' "$@" > "$LIST"
elif git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git ls-files -z | while IFS= read -r -d '' file; do
    [ "$file" = scripts/scan-coordinates.sh ] || printf '%s\0' "$file"
  done > "$LIST"
else
  find . -type f -not -path './.git/*' -print0 | while IFS= read -r -d '' file; do
    file="${file#./}"
    [ "$file" = scripts/scan-coordinates.sh ] || printf '%s\0' "$file"
  done > "$LIST"
fi

scan() {
  local label="$1" pattern="$2" file rc
  while IFS= read -r -d '' file; do
    rc=0
    printf '%s\n' "$file" | grep -anEo -- "$pattern" 2> "$REPORT.error" |
      awk -v file="$file" -v label="$label" '{
        match($0, /^[0-9]+:/)
        print file ":path: " label ": " substr($0, RLENGTH + 1)
      }' >> "$REPORT" || rc=$?
    case "$rc" in
      0|1) ;;
      *)
        cat "$REPORT.error" >&2
        return "$rc"
        ;;
    esac

    rc=0
    grep -anEo -- "$pattern" "$file" 2> "$REPORT.error" |
      awk -v file="$file" -v label="$label" '{
        match($0, /^[0-9]+:/)
        print file ":" substr($0, 1, RLENGTH) " " label ": " substr($0, RLENGTH + 1)
      }' >> "$REPORT" || rc=$?
    case "$rc" in
      0|1) ;;
      *)
        cat "$REPORT.error" >&2
        return "$rc"
        ;;
    esac
  done < "$LIST"
}

drop() {
  local rc=0
  grep -vE -- "$1" "$REPORT" > "$REPORT.filtered" || rc=$?
  case "$rc" in
    0|1) ;;
    *)
      echo "cannot filter coordinate report" >&2
      return "$rc"
      ;;
  esac
  mv "$REPORT.filtered" "$REPORT"
}

# 1. Routable IPv4 literals. Documentation, private, loopback, link-local,
#    multicast and broadcast ranges plus well-known public resolvers are fine.
scan "routable IPv4" '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b'
drop ' routable IPv4: (10\.|127\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.|192\.168\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|22[4-9]\.|23[0-9]\.|0\.0\.0\.0|255\.)'
drop ' routable IPv4: (1\.1\.1\.1|1\.0\.0\.1|8\.8\.8\.8|8\.8\.4\.4|9\.9\.9\.9)$'

# 2. Key material.
scan "age recipient" 'age1[0-9a-z]{50,}'
scan "SSH public key" 'ssh-(ed25519|rsa|dss) AAAA[A-Za-z0-9+/]{20,}'
scan "private key block" 'BEGIN [A-Z ]*PRIVATE KEY'
scan "base64 key (32 bytes)" '\b[A-Za-z0-9+/]{43}='
# Terraform provider hashes have the same shape and are not key material.
drop '^terraform/\.terraform\.lock\.hcl:[0-9]+: base64 key'

# 3. Client credentials.
scan "UUID" '\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'

# Placeholders built from one repeated character (00000000-…, AAAA…=) are not
# credentials; everything else is.
awk '
function uniform(s,   i, first) {
  first = substr(s, 1, 1)
  for (i = 2; i <= length(s); i++) if (substr(s, i, 1) != first) return 0
  return 1
}
{
  value = $NF
  if (index($0, " UUID: ") > 0 && uniform(substr(value, 1, 8))) next
  if (index($0, " base64 key (32 bytes): ") > 0) {
    body = value
    gsub(/=/, "", body)
    if (uniform(body)) next
  }
  print
}' "$REPORT" > "$REPORT.filtered"
mv "$REPORT.filtered" "$REPORT"

if [ -s "$REPORT" ]; then
  echo "coordinates found — this repository must not carry them:" >&2
  sed 's/^/  /' "$REPORT" >&2
  exit 1
fi

echo "scan-coordinates: clean"
