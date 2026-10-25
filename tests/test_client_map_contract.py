"""Client declaration checks shared by deployment and the real exporter."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHECK = "Assert inventory and SOPS declare the same clients"


def test_every_secret_map_consumer_inherits_validation(tmp_path):
    mapping = yaml.safe_load((ROOT / "roles/singbox/tasks/secrets_map.yml").read_text())
    mapped = next(i for i, task in enumerate(mapping) if "node_clients" in task.get("ansible.builtin.set_fact", {}))
    def validated(tasks):
        return any(CHECK == task.get("name") for task in tasks[mapped + 1 :])

    assert validated(mapping)
    assert not validated([task for task in mapping if task.get("name") != CHECK])

    def direct_sops_client_reads(root):
        """Find per-node client keys, including Jinja concatenation and aliases."""
        found = set()
        for directory in ("roles", "playbooks"):
            for path in (root / directory).rglob("*"):
                if path.suffix not in (".yml", ".yaml", ".j2"):
                    continue
                if path == root / "roles/singbox/tasks/secrets_map.yml":
                    continue
                source = "\n".join(line for line in path.read_text().splitlines()
                                   if not line.lstrip().startswith("#"))
                if re.search(r"node_[A-Za-z0-9_-]*_clients\b|node_\{\{[^}]+\}\}_clients|node_[\'\"]\s*~[\s\S]{0,100}_clients|\b_clients_var\b", source):
                    found.add(str(path.relative_to(root)))
        return found

    assert direct_sops_client_reads(ROOT) == set()
    (tmp_path / "playbooks").mkdir()
    bypass = tmp_path / "playbooks/bypass.yml"
    bypass.write_text("- ansible.builtin.set_fact:\n    clients: \"{{ lookup('vars', 'node_' ~ node_name ~ '_clients') }}\"\n")
    assert direct_sops_client_reads(tmp_path) == {"playbooks/bypass.yml"}
    bypass.write_text("- ansible.builtin.set_fact:\n    clients: >-\n      {{ lookup('vars', 'node_' ~\n        node_name ~ '_clients') }}\n")
    assert direct_sops_client_reads(tmp_path) == {"playbooks/bypass.yml"}


@pytest.mark.parametrize("route", ["deploy", "export"])
@pytest.mark.parametrize("case", ["matched", "inventory_only", "sops_only", "undeclared"])
def test_client_map_blocks_divergence_before_artifacts(tmp_path, route, case):
    assert shutil.which("ansible-playbook"), "Ansible is required; skip is not success"
    inventory = tmp_path / "inventory.yml"
    names = {"matched": ["alice"], "inventory_only": ["alice", "bob"],
             "sops_only": ["alice"], "undeclared": None}[case]
    clients = [{"name": "alice", "uuid": "11111111-1111-4111-8111-111111111111"}]
    if case == "sops_only":
        clients.append({"name": "bob", "uuid": "22222222-2222-4222-8222-222222222222"})
    host = {"ansible_connection": "local", "ansible_python_interpreter": "/usr/bin/python3",
            "ansible_host": "127.0.0.1", "node_name": "synthetic"}
    if names is not None:
        host["singbox_client_names"] = names
    inventory.write_text(yaml.safe_dump({"all": {"hosts": {"synthetic": host}}}))
    clients_dir = tmp_path / "clients"
    playbook = ROOT / ("tests/client-map-deploy.yml" if route == "deploy" else "playbooks/export-clients.yml")
    variables = {"node_synthetic_clients": clients, "clients_dir": str(clients_dir),
                 "client_filter": "alice", "singbox_hy2_enabled": False}
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(playbook), "-e", json.dumps(variables)],
        cwd=ROOT, env={**os.environ, "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg"),
                       "ANSIBLE_ROLES_PATH": str(ROOT / "roles")},
        capture_output=True, text=True, timeout=90,
    )
    output = proc.stdout + proc.stderr
    assert CHECK in output, f"{route}/{case}: shared check was not reached"
    if case == "matched":
        check_result = re.search(
            r"TASK \[(?:singbox : )?" + CHECK + r"\](.*?)(?=\nTASK \[|\nPLAY RECAP|\Z)",
            output, re.S,
        )
        assert check_result and "ok: [synthetic]" in check_result.group(1)
        assert "inventory only:" not in output
    else:
        assert proc.returncode != 0
        assert "synthetic: client names differ" in output
        assert ("missing singbox_client_names" in output) if case == "undeclared" else True
    assert not clients_dir.exists() or not list(clients_dir.rglob("*"))
