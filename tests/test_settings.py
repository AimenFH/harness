"""Tests for the settings file (--settings) and the --sandbox option."""

from pathlib import Path

import pytest

from config import SETTINGS_KEYS, Config, load_settings
from main import parse_config

EXAMPLE = Path(__file__).resolve().parent.parent / "settings.example.toml"


def args(root, *extra):
    return ["--root", str(root), "--task", "Fix it", "--mode", "edit", "--model", "m", *extra]


def write_settings(tmp_path, text):
    path = tmp_path / "settings.toml"
    path.write_text(text)
    return str(path)


def test_example_settings_are_the_defaults():
    # If a default changes, settings.example.toml must change with it.
    settings = load_settings(EXAMPLE)
    defaults = Config(root=Path("."), task="t", mode="edit", model="m")

    assert sorted(settings) == sorted(SETTINGS_KEYS)
    for key, value in settings.items():
        assert getattr(defaults, key) == value, key


def test_settings_file_changes_limits(tmp_path):
    path = write_settings(tmp_path, "max_actions = 5\ncommand_timeout_seconds = 90\n")
    config = parse_config(args(tmp_path, "--settings", path))

    assert config.max_actions == 5
    assert config.command_timeout_seconds == 90
    assert config.max_retries == 3   # untouched settings keep their default


def test_sandbox_defaults_to_docker(tmp_path):
    config = parse_config(args(tmp_path))

    assert config.sandbox == "docker"
    assert "Sandbox: docker (image harness-sandbox)" in config.summary()


def test_command_line_sandbox_wins_over_settings_file(tmp_path):
    path = write_settings(tmp_path, 'sandbox = "docker"\n')
    config = parse_config(args(tmp_path, "--settings", path, "--sandbox", "none"))

    assert config.sandbox == "none"
    assert "NOT contained" in config.summary()


@pytest.mark.parametrize("text, message", [
    ("api_key = 'x'\n", "unknown setting 'api_key'"),
    ("max_actions = 0\n", "whole number above 0"),
    ("max_actions = '20'\n", "whole number above 0"),
    ("max_actions = true\n", "whole number above 0"),
    ("sandbox = 'vm'\n", "must be one of docker, none"),
    ("sandbox_image = ''\n", "non-empty text"),
    ("max_actions = \n", "not valid TOML"),
])
def test_bad_settings_are_rejected(tmp_path, capsys, text, message):
    path = write_settings(tmp_path, text)
    with pytest.raises(SystemExit) as error:
        parse_config(args(tmp_path, "--settings", path))

    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_missing_settings_file_is_rejected(tmp_path, capsys):
    with pytest.raises(SystemExit):
        parse_config(args(tmp_path, "--settings", str(tmp_path / "nope.toml")))

    assert "cannot read settings file" in capsys.readouterr().err
