#!/usr/bin/env python3
"""Reject nonempty defaults consumed by any UFW opening rule in the role."""
import re
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def unsafe_defaults(role: Path) -> list[str]:
    consumed = set()
    for path in (role / "tasks").rglob("*"):
        if path.suffix not in (".yml", ".yaml"):
            continue
        for task in yaml.safe_load(path.read_text()) or []:
            ufw = task.get("community.general.ufw")
            if not isinstance(ufw, dict) or "rule" not in ufw or ufw.get("rule") in ("deny", "reject"):
                continue
            consumed.update(re.findall(r"\b(firewall_\w+)\b", str(task)))
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    return sorted(name for name in consumed if name in defaults and isinstance(defaults[name], list) and defaults[name])


def check_mutations(role: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = Path(tmp)
        for source in (role / "tasks").rglob("*"):
            if source.suffix not in (".yml", ".yaml"):
                continue
            target = fixture / source.relative_to(role)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text())
        target = fixture / "defaults/main.yml"
        target.parent.mkdir(parents=True, exist_ok=True)
        original = (role / "defaults/main.yml").read_text()
        for filename, rule, field in (
            ("extra.yml", "allow", "loop: '{{ firewall_fixture_ports }}'"),
            ("extra.yml", "limit", "loop: '{{ firewall_fixture_ports }}'"),
            ("extra.yml", "allow", "port: '{{ firewall_fixture_ports }}'"),
        ):
            (fixture / "tasks/extra.yml").write_text(
                f"- community.general.ufw:\n    rule: {rule}\n  {field}\n"
            )
            target.write_text(original + "\nfirewall_fixture_ports: [9999]\n")
            assert unsafe_defaults(fixture) == ["firewall_fixture_ports"]


def main() -> int:
    role = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "roles/firewall"
    unsafe = unsafe_defaults(role)
    if unsafe:
        print("FAIL: firewall rule defaults are nonempty: " + ", ".join(unsafe))
        return 1
    if len(sys.argv) == 1:
        check_mutations(role)
        print("firewall defaults: all consumed rule defaults empty or undeclared; three mutations rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
