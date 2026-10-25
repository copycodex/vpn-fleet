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


def test_public_workflow_scans_repository_files_only():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["validate"]
    steps = job["steps"]
    named = {step["name"]: step for step in steps if "name" in step}

    assert job["env"]["ANSIBLE_COLLECTIONS_PATH"] == "/tmp/ansible-collections"
    assert 'ansible-galaxy collection install -r requirements.yml -p "$ANSIBLE_COLLECTIONS_PATH"' in named["Ansible and YAML lint"]["run"]
    assert named["Scan secrets"]["run"].splitlines()[-1].strip() == (
        "/tmp/gitleaks git . --no-banner --redact --exit-code 1"
    )
    assert named["Scan coordinates"]["run"].strip() == "scripts/scan-coordinates.sh"

    checkout = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/checkout@"))
    assert steps[checkout + 1]["run"].strip() == (
        'git config --system --add safe.directory "$GITHUB_WORKSPACE"'
    )
    public_ignore = ROOT / "publication/overlay/.gitignore"
    if not public_ignore.exists():
        public_ignore = ROOT / ".gitignore"
    assert ".ansible/" in public_ignore.read_text(encoding="utf-8").splitlines()


def test_container_jobs_run_with_bash():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        if "container" in job:
            assert job.get("defaults", {}).get("run", {}).get("shell") == "bash"


def test_public_workflow_inputs_exist():
    """Run this test in the exported snapshot to catch private-only inputs."""
    for path in (
        ".github/workflows/ci.yml",
        ".ansible-lint",
        ".yamllint",
        "ansible.cfg",
        "requirements-ci.txt",
        "requirements.yml",
        "roles/singbox/defaults/main.yml",
        "scripts/scan-coordinates.sh",
        "tests/inventory/nodes/ci.yml",
        "tests/render-singbox-config.yml",
    ):
        assert (ROOT / path).is_file(), path

    assert (ROOT / "tests/inventory").is_dir()
    assert list((ROOT / "playbooks").glob("*.yml"))
    assert list((ROOT / "tests").glob("test_*.py"))
    assert list(ROOT.rglob("*.sh"))
