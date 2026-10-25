"""CI must fail when a discovered test silently skips."""

import os

import pytest


_skipped = 0


def pytest_runtest_logreport(report):
    global _skipped
    if report.skipped:
        _skipped += 1


def pytest_collectreport(report):
    global _skipped
    if report.skipped:
        _skipped += 1


def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("CI") == "true" and _skipped:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"CI forbids skipped tests: {_skipped}", red=True)
