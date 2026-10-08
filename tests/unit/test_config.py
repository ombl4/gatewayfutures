from pathlib import Path

from typer.testing import CliRunner

from gf.cli import app
from gf.config import ROOT, Thresholds, thresholds


def test_thresholds_file_matches_model_defaults():
    """thresholds.yaml must name only known keys; defaults in code mirror the file."""
    loaded = thresholds(ROOT / "thresholds.yaml")
    assert loaded == Thresholds(**loaded.model_dump())
    assert loaded.latency_p95_warn_s == 2.0


def test_thresholds_missing_file_uses_defaults(tmp_path: Path):
    assert thresholds(tmp_path / "nope.yaml") == Thresholds()


def test_cli_lists_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "doctor" in result.output
