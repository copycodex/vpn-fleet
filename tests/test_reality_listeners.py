"""The deployed frontend needs both its backend and public listeners."""

from pathlib import Path
import shutil
import socket
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles" / "reality_frontend" / "tasks" / "main.yml"


@pytest.mark.skipif(not shutil.which("ansible-playbook"), reason="ansible-playbook unavailable")
@pytest.mark.parametrize("missing", [None, "backend", "public"])
def test_both_listener_checks_are_required(tmp_path: Path, missing: str | None) -> None:
    # Execute the role's actual wait_for tasks with synthetic loopback sockets.
    # Removing either task makes its corresponding missing-listener run pass.
    tasks = [task for task in yaml.safe_load(TASKS.read_text(encoding="utf-8"))
             if "ansible.builtin.wait_for" in task]
    assert tasks
    for task in tasks:
        task["ansible.builtin.wait_for"]["timeout"] = 1

    listeners = {}
    try:
        for name in ("backend", "public"):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listeners[name] = listener
        ports = {name: sock.getsockname()[1] for name, sock in listeners.items()}
        if missing:
            listeners[missing].close()
        playbook = tmp_path / "listeners.yml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost", "gather_facts": False,
            "vars": {
                "reality_frontend_backend_host": "127.0.0.1",
                "reality_frontend_backend_port": ports["backend"],
                "reality_frontend_bind_port": ports["public"],
            },
            "tasks": tasks,
        }]), encoding="utf-8")
        result = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
            capture_output=True, text=True, check=False,
        )
        assert (result.returncode == 0) == (missing is None), (
            f"missing={missing}, rc={result.returncode}\n{result.stdout[-1500:]}"
        )
    finally:
        for listener in listeners.values():
            listener.close()
