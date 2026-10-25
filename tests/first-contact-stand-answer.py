#!/usr/bin/env python3
"""Measure first-contact against four synthetic SSH/PAM containers."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pexpect
import yaml


WORK = Path(sys.argv[1])
ROOT = Path(sys.argv[2])
sys.path.insert(0, str(ROOT / "scripts"))
from secrets_lock import secrets_lock  # noqa: E402

PRIVATE = WORK / "private"
KEY = WORK / "age-key.txt"
FLEET_KEY = WORK / "fleet-key"
PROVIDER = "Stand-Initial9!Password"
CASES = {"A1": 22131, "A2": 22132, "B1": 22133, "B2": 22134}


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, env=env, text=True, capture_output=True, check=True)


def setup() -> None:
    (PRIVATE / "inventories/nodes").mkdir(parents=True)
    (PRIVATE / "inventories/group_vars/all").mkdir(parents=True)
    (PRIVATE / "inventories/known_hosts").touch()
    run("age-keygen", "-o", str(KEY))
    recipient = run("age-keygen", "-y", str(KEY)).stdout.strip()
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(FLEET_KEY))
    (PRIVATE / ".sops.yaml").write_text(
        "creation_rules:\n"
        "  - path_regex: first-entry[.]sops[.]yml$\n"
        "    encrypted_regex: ^password$\n"
        f"    age: {recipient}\n"
        "  - path_regex: inventories/group_vars/all/secrets[.]sops[.]yml$\n"
        f"    age: {recipient}\n"
    )
    entries = {}
    secrets = {}
    for case, port in CASES.items():
        (PRIVATE / f"inventories/nodes/{case}.yml").write_text(
            "all:\n  hosts:\n"
            f"    {case}:\n"
            "      ansible_host: 127.0.0.1\n"
            "      ansible_user: fresh\n"
            f"      ansible_port: {port}\n"
            f"      ansible_ssh_private_key_file: {FLEET_KEY}\n"
            f"      node_name: {case}\n"
        )
        entries[case] = {
            "user": "fresh", "port": port, "method": "password", "password": PROVIDER,
        }
        secrets[f"node_{case}_console_password"] = ""
    entry_file = PRIVATE / "inventories/first-entry.sops.yml"
    secrets_file = PRIVATE / "inventories/group_vars/all/secrets.sops.yml"
    entry_file.write_text(yaml.safe_dump({"first_entry": entries}, sort_keys=False))
    secrets_file.write_text(yaml.safe_dump(secrets, sort_keys=False))
    env = dict(os.environ, SOPS_AGE_KEY_FILE=str(KEY))
    for path in (entry_file, secrets_file):
        with secrets_lock(path):
            run("sops", "--config", str(PRIVATE / ".sops.yaml"),
                "--encrypt", "--in-place", str(path), env=env)

    # Only the exact one-byte nudge is recorded. Passwords and SSH output are not logged.
    (WORK / "sitecustomize.py").write_text(
        "import os\nimport pexpect\n"
        "_send = pexpect.spawn.send\n"
        "def send(self, text):\n"
        "    if text.endswith('\\n'):\n"
        "        self._answer_stand_lines = getattr(self, '_answer_stand_lines', 0) + 1\n"
        "    if text == '\\r':\n"
        "        with open(os.environ['ANSWER_STAND_NUDGE_LOG'], 'a') as log:\n"
        "            log.write(str(getattr(self, '_answer_stand_lines', 0)) + '\\n')\n"
        "    return _send(self, text)\n"
        "pexpect.spawn.send = send\n"
    )


def saved(case: str) -> dict[str, str]:
    env = dict(os.environ, SOPS_AGE_KEY_FILE=str(KEY))
    output = run(
        "sops", "--config", str(PRIVATE / ".sops.yaml"), "--decrypt",
        str(PRIVATE / "inventories/group_vars/all/secrets.sops.yml"), env=env,
    ).stdout
    data = yaml.safe_load(output)
    return {
        "console": data.get(f"node_{case}_console_password", ""),
        "pending": data.get(f"node_{case}_console_password_pending", ""),
    }


def password_opens(port: int, password: str) -> bool:
    child = pexpect.spawn(
        "ssh", ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
                "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                "-o", "NumberOfPasswordPrompts=1", "-p", str(port),
                "fresh@127.0.0.1", "printf ANSWER_STAND_OK"],
        encoding="utf-8", timeout=8,
    )
    try:
        if child.expect(["password:", pexpect.EOF, pexpect.TIMEOUT]) != 0:
            return False
        child.send(password + "\n")
        result = child.expect(["ANSWER_STAND_OK", "Permission denied", pexpect.EOF,
                               pexpect.TIMEOUT])
        return result == 0
    finally:
        child.close(force=True)


def key_opens(port: int) -> bool:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
         "-o", "UserKnownHostsFile=/dev/null", "-o", "PreferredAuthentications=publickey",
         "-i", str(FLEET_KEY), "-p", str(port), "fresh@127.0.0.1", "true"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False,
    )
    return result.returncode == 0


def measure(case: str, port: int) -> bool:
    nudge_log = WORK / f"{case}-nudge"
    env = dict(os.environ, SOPS_AGE_KEY_FILE=str(KEY), VPN_FLEET_PRIVATE_DIR=str(PRIVATE),
               ANSWER_STAND_NUDGE_LOG=str(nudge_log), PYTHONPATH=str(WORK))
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/first-contact.py"), case, "--apply"],
        env=env, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=100, check=False,
    )
    values = saved(case)
    candidates = [("provider", PROVIDER)]
    if values["pending"]:
        candidates.append(("pending", values["pending"]))
    if values["console"] and values["console"] != values["pending"]:
        candidates.append(("console", values["console"]))
    actual = "other:unidentified"
    for label, password in candidates:
        if password_opens(port, password):
            actual = label
            break
    if actual.startswith("other:") and values["pending"]:
        for form, password in (
            ("pending+CR", values["pending"] + "\r"),
            ("pending+NL", values["pending"] + "\n"),
            ("CR+pending", "\r" + values["pending"]),
        ):
            if password_opens(port, password):
                actual = f"other:{form}"
                break
    if result.returncode == 0:
        valid = (actual == "console" and bool(values["console"])
                 and key_opens(port) and not values["pending"])
    else:
        valid = (actual in ("provider", "pending") and bool(values["pending"]))
    nudge_stages = nudge_log.read_text().splitlines() if nudge_log.is_file() else []
    # Login sends line 1; current password is line 2. The slow PAM cases must
    # exercise the requested gap, not an unrelated wait later in passwd.
    if case in ("A1", "A2"):
        valid = valid and "2" in nudge_stages
    nudge = "да" if nudge_stages else "нет"
    verdict = "ok" if valid else "DEFECT"
    print(f"answer-stand {case}: rc={result.returncode} actual={actual} "
          f"nudge={nudge} verdict={verdict}", flush=True)
    return valid


def main() -> int:
    setup()
    valid = [measure(case, port) for case, port in CASES.items()]
    return 0 if all(valid) else 1


if __name__ == "__main__":
    raise SystemExit(main())
