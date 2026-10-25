"""Pure bootstrap preflight tests; no SSH or playbook execution."""

import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]


def _module():
    spec = importlib.util.spec_from_file_location("bootstrap_test", ROOT / "wizard/bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_key_method_entry_still_blocks_bootstrap():
    bootstrap = _module()

    class Door:
        def inspect_door(self, *_):
            return type("Result", (), {"state": "ключ парка объявлен и читается", "reasons": ()})()
        def _inventory_key(self, *_):
            return "synthetic-node", True, []
        def _entry_for_node(self, *_):
            return True, {"method": "key"}, "synthetic-map", []

    reason = bootstrap.check_first_contact(Door(), Path("/synthetic"), "synthetic-node")
    assert reason is not None and "first-contact" in reason


def test_repeat_bootstrap_skips_current_ssh_listener(monkeypatch):
    bootstrap = _module()
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SSH must not run")))
    host = {
        "ansible_host": "stand.invalid", "ansible_user": "devops", "ansible_port": 2424,
        "bootstrap_ssh_port": 2424, "ansible_ssh_private_key_file": "/synthetic/key",
    }
    allowed, message = bootstrap.check_port_on_node(None, Path("/synthetic"), host, "node", None)
    assert allowed and "текущей SSH-дверью" in message
def test_ssh_connection_survives_transient_path_loss():
    from configparser import ConfigParser
    from pathlib import Path
    import re
    import subprocess
    import os
    import tempfile

    root = Path(__file__).resolve().parents[1]
    config = ConfigParser(interpolation=None)
    config.read(root / "ansible.cfg")
    assert config.getint("ssh_connection", "retries") >= 3
    args = config.get("ssh_connection", "ssh_args")
    assert re.search(r"ServerAliveInterval=5(?:\s|$)", args)
    assert re.search(r"ServerAliveCountMax=3(?:\s|$)", args)
    with tempfile.TemporaryDirectory() as ansible_tmp:
        changed = subprocess.run(
            ["ansible-config", "dump", "--only-changed", "-t", "connection",
             "ansible.builtin.ssh"], cwd=root,
            env={**os.environ, "ANSIBLE_LOCAL_TEMP": ansible_tmp},
            capture_output=True, text=True, check=True,
        ).stdout
    assert "reconnection_retries(" in changed and "= 6" in changed
    assert "ssh_args(" in changed and "ServerAliveInterval=5" in changed
    assert "timeout: 90" in (root / "playbooks" / "bootstrap.yml").read_text()
