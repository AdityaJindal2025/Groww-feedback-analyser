from typer.testing import CliRunner

import pulse
from pulse.cli import app


def test_package_imports() -> None:
    assert pulse.__version__


def test_cli_help_lists_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ingest" in result.output
