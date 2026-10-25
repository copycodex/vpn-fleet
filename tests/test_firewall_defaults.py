"""Run the role-wide default contract in the pytest CI job."""
from pathlib import Path
import runpy


def test_firewall_defaults_and_mutations():
    checker = runpy.run_path(str(Path(__file__).with_name("firewall-defaults.py")))
    role = Path(__file__).resolve().parents[1] / "roles/firewall"
    assert checker["unsafe_defaults"](role) == []
    checker["check_mutations"](role)
