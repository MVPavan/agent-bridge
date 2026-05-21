"""Smoke test — proves the package imports and the CLI is wired."""

from __future__ import annotations

from click.testing import CliRunner

from agent_bridge import __version__
from agent_bridge.cli import main


def test_version_is_a_string() -> None:
    assert isinstance(__version__, str)
    assert __version__.count(".") >= 1


def test_cli_status_runs() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output
