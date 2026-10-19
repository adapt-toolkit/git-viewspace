"""Make any skipped or xfail-like pytest outcome fail a harness case."""

from __future__ import annotations

from typing import Any

import pytest


_DISALLOWED_OUTCOMES: set[str] = set()
_COLLECTED_ITEMS: set[str] = set()
_TERMINAL_ITEMS: set[str] = set()


def pytest_sessionstart(session: pytest.Session) -> None:
    _DISALLOWED_OUTCOMES.clear()
    _COLLECTED_ITEMS.clear()
    _TERMINAL_ITEMS.clear()


def pytest_collection_finish(session: pytest.Session) -> None:
    _COLLECTED_ITEMS.update(item.nodeid for item in session.items)


def pytest_runtest_logreport(report: Any) -> None:
    if report.when == "call" or (report.when == "setup" and not report.passed):
        _TERMINAL_ITEMS.add(report.nodeid)
    if report.skipped or getattr(report, "wasxfail", False):
        _DISALLOWED_OUTCOMES.add(report.nodeid)


def pytest_collectreport(report: Any) -> None:
    if report.skipped:
        _DISALLOWED_OUTCOMES.add(report.nodeid)


def pytest_deselected(items: list[Any]) -> None:
    _DISALLOWED_OUTCOMES.update(item.nodeid for item in items)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    nonexecuted = _COLLECTED_ITEMS - _TERMINAL_ITEMS
    forbidden = _DISALLOWED_OUTCOMES | nonexecuted
    if not forbidden:
        return
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")
    if terminal is not None:
        terminal.write_sep(
            "=",
            "VIEWSPACE forbids skipped/xfail/nonexecuted pytest outcomes: "
            + ", ".join(sorted(forbidden)),
            red=True,
        )
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
