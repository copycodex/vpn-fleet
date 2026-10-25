"""Decision gate for dependency installation, without running the installer."""

import os
from pathlib import Path
import pty
import subprocess


LIB = Path(__file__).resolve().parents[1] / "wizard" / "lib.sh"


def _confirm(assume_yes: int, answer: str | None = None):
    master = slave = None
    try:
        if answer is not None:
            master, slave = pty.openpty()
            os.write(master, answer.encode() + b"\n")
        return subprocess.run(
            ["bash", "-c", 'source "$LIB_PATH"; confirm_install'],
            env={**os.environ, "LIB_PATH": str(LIB), "ASSUME_YES": str(assume_yes)},
            stdin=slave if slave is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        if master is not None:
            os.close(master)
            os.close(slave)


def test_yes_accepts_without_tty():
    assert _confirm(1).returncode == 0


def test_no_tty_without_yes_refuses_clearly():
    result = _confirm(0)
    assert result.returncode != 0
    assert "TTY" in result.stderr and "--yes" in result.stderr


def test_tty_asks_and_obeys_answer():
    assert _confirm(0, "y").returncode == 0
    rejected = _confirm(0, "n")
    assert rejected.returncode != 0
    assert "отменена" in rejected.stderr
