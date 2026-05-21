"""agent-bridge CLI entrypoint."""

from __future__ import annotations

import click

from agent_bridge import __version__


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
def main() -> None:
    """agent-bridge: bidirectional Claude-Claude-Codex development bridge."""


@main.command()
def status() -> None:
    """Print broker status. Stub until CP3+ wires the real state store."""
    click.echo(f"agent-bridge v{__version__} (CP3 stub)")


if __name__ == "__main__":
    main()
