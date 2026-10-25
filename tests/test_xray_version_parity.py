"""Keep the CI Xray release aligned with public defaults and node inventories."""

from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CI_INVENTORY = ROOT / "tests/inventory/nodes/ci.yml"
DEFAULTS = ROOT / "roles/singbox/defaults/main.yml"
EXAMPLE = ROOT / "inventories/nodes/example.yml.template"


def _load_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        pytest.fail(f"invalid YAML in {path.name}", pytrace=False)




def test_public_xray_version_and_checksums_match():
    ci_version = _load_yaml(CI_INVENTORY)["all"]["hosts"]["ci"]["xray_version"]
    defaults = _load_yaml(DEFAULTS)
    example_version = _load_yaml(EXAMPLE)["all"]["hosts"]["example"]["xray_version"]

    assert ci_version == defaults["xray_version"] == example_version, (
        f"Xray versions differ: CI={ci_version}, defaults={defaults['xray_version']}, "
        f"example={example_version}"
    )
    checksums = defaults["xray_checksums"].get(ci_version, {})
    assert all(checksums.get(arch) for arch in ("amd64", "arm64")), (
        f"Xray {ci_version} lacks amd64 or arm64 checksum"
    )


