"""The public snapshot must retain its independent CI boundary checks."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/ci.yml"


def test_public_workflow_scans_coordinates_and_all_shell_scripts():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"].values()
    assert workflow["permissions"] == {"contents": "read"}

    steps = [step for job in jobs for step in job["steps"]]
    gates = [step for step in steps if step.get("run", "").strip() == "scripts/scan-coordinates.sh"]
    assert len(gates) == 1
    assert "continue-on-error" not in gates[0]
    assert all("continue-on-error" not in job for job in workflow["jobs"].values())

    lint_steps = [step for step in steps if "xargs -0 shellcheck" in step.get("run", "")]
    assert len(lint_steps) == 1
    lint = lint_steps[0]["run"]
    assert "find . -path './.git' -prune -o -type f -name '*.sh' -print0" in lint
    assert "[ ! -s \"$files\" ]" in lint
    assert "xargs -0 shellcheck < \"$files\"" in lint

    for job in workflow["jobs"].values():
        runners = job["runs-on"]
        if isinstance(runners, str):
            runners = [runners]
        assert "self-hosted" not in runners
