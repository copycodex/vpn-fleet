#!/usr/bin/env python3
"""Build synthetic Ansible variables from the RFC 7748 Alice vector."""

import base64
import json
import sys


def encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


private_key = bytes.fromhex(
    "77076d0a7318a57d3c16c17251b26645"
    "df4c2f87ebc0992ab177fba51db92c2a"
)
public_key = bytes.fromhex(
    "8520f0098930a754748b7ddcb43ef75a"
    "0dbf3a0d26381af4eba4a98eaa9b4e6a"
)
if sys.argv[1:] == ["--wrong-public"]:
    public_key = bytes([public_key[0] ^ 1]) + public_key[1:]
elif sys.argv[1:]:
    raise SystemExit("unknown fixture option")

print(
    json.dumps(
        {
            "private_key": encoded(private_key),
            "public_key": encoded(public_key),
            "node_ci_reality_private_key": encoded(private_key),
            "node_ci_reality_public_key": encoded(public_key),
            "node_ci_clients": [
                {"name": "ci-client", "uuid": "11111111-1111-4111-8111-111111111111"}
            ],
        }
    )
)
