#!/usr/bin/env python3
"""One advisory lock for each SOPS transaction target."""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from typing import Iterator


LOCKS_ENV = "VPN_FLEET_SOPS_LOCK_IDENTITIES"


def lock_path_for(target: Path) -> Path:
    identity = hashlib.sha256(os.fsencode(target.resolve())).hexdigest()
    return Path(tempfile.gettempdir()) / f"vpn-fleet-sops-{os.getuid()}-{identity}.lock"


@contextmanager
def secrets_lock(target: Path) -> Iterator[None]:
    identity = lock_path_for(target).stem
    held = os.environ.get(LOCKS_ENV, "").split(":")
    if identity in held:
        yield
        return
    with lock_path_for(target).open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = os.environ.get(LOCKS_ENV)
        os.environ[LOCKS_ENV] = ":".join(filter(None, [previous, identity]))
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop(LOCKS_ENV, None)
            else:
                os.environ[LOCKS_ENV] = previous
            fcntl.flock(lock, fcntl.LOCK_UN)


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print("usage: secrets_lock.py TARGET -- COMMAND [ARG ...]", file=sys.stderr)
        return 2
    with secrets_lock(Path(sys.argv[1])):
        return subprocess.call(sys.argv[3:])


if __name__ == "__main__":
    raise SystemExit(main())
