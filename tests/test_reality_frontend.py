"""Rendered HAProxy configuration invariants."""

from pathlib import Path
import jinja2
import yaml


TEMPLATE = Path(__file__).resolve().parents[1] / "roles/reality_frontend/templates/haproxy.cfg.j2"


def test_idle_reality_tunnel_is_longer_than_regular_io_timeout():
    rendered = jinja2.Template(TEMPLATE.read_text()).render(
        reality_frontend_bind_port=443,
        reality_frontend_inspect_delay="5s",
        reality_frontend_backend_host="127.0.0.1",
        reality_frontend_backend_port=1443,
    )
    assert "timeout tunnel 1h" in rendered
    assert "timeout client 1m" in rendered
    assert "timeout server 1m" in rendered


def test_frontend_is_reloaded_before_public_bind_check():
    tasks = yaml.safe_load((TEMPLATE.parents[1] / "tasks/main.yml").read_text())
    flush = next(index for index, task in enumerate(tasks)
                 if task.get("ansible.builtin.meta") == "flush_handlers")
    checks = [(index, task["ansible.builtin.wait_for"])
              for index, task in enumerate(tasks) if "ansible.builtin.wait_for" in task]
    assert checks and all(index > flush for index, _ in checks)
    assert any(check["port"] == "{{ reality_frontend_bind_port }}" for _, check in checks)
