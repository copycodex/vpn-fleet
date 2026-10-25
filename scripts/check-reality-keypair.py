#!/usr/bin/env python3
"""Compare an Xray Reality private key with the exported public key.

Input is JSON on stdin so the private key never enters process arguments.
Only a fixed status message is written; key material is never echoed.
"""

import base64
import binascii
import hmac
import json
import sys


def decode_key(value: str) -> bytes:
    if not isinstance(value, str) or len(value) != 43:
        raise ValueError("invalid key")
    raw = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    if len(raw) != 32:
        raise ValueError("invalid key")
    return raw


def x25519_public(private_key: bytes) -> bytes:
    """RFC 7748 X25519 scalar multiplication with the base point 9."""
    prime = 2**255 - 19
    scalar = int.from_bytes(private_key, "little")
    scalar &= (1 << 255) - 8
    scalar |= 1 << 254
    x1, x2, z2, x3, z3, swap = 9, 1, 0, 9, 1, 0
    for bit in range(254, -1, -1):
        current = (scalar >> bit) & 1
        swap ^= current
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = current
        a = (x2 + z2) % prime
        aa = a * a % prime
        b = (x2 - z2) % prime
        bb = b * b % prime
        e = (aa - bb) % prime
        c = (x3 + z3) % prime
        d = (x3 - z3) % prime
        da = d * a % prime
        cb = c * b % prime
        x3 = (da + cb) ** 2 % prime
        z3 = x1 * (da - cb) ** 2 % prime
        x2 = aa * bb % prime
        z2 = e * (aa + 121665 * e) % prime
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, prime - 2, prime) % prime).to_bytes(32, "little")


def main() -> int:
    try:
        values = json.load(sys.stdin)
        private_key = decode_key(values["private_key"])
        public_key = decode_key(values["public_key"])
        if not hmac.compare_digest(x25519_public(private_key), public_key):
            raise ValueError("mismatched key")
    except (ValueError, KeyError, TypeError, binascii.Error):
        print("Reality key pair check failed", file=sys.stderr)
        return 1
    print("Reality key pair check: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
