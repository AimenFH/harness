"""Tests for command-line parsing and validation in main.py."""

import pytest

from main import main, parse_config


def make_args(root, **overrides):
    """Build a valid argument list, optionally replacing some values."""
    values = {
        "--root": str(root),
        "--task": "Fix the incorrect total",
        "--mode": "readonly",
        "--model": "llama3.1",
    }
    values.update(overrides)
    args = []
    for option, value in values.items():
        args.extend([option, value])
    return args


def test_valid_arguments_create_config(tmp_path):
    config = parse_config(make_args(tmp_path))

    assert config.root == tmp_path.resolve()
    assert config.task == "Fix the incorrect total"
    assert config.mode == "readonly"
    assert config.model == "llama3.1"
    assert config.offline is False


def test_edit_mode_is_accepted(tmp_path):
    config = parse_config(make_args(tmp_path, **{"--mode": "edit"}))
    assert config.mode == "edit"


def test_offline_flag(tmp_path):
    config = parse_config(make_args(tmp_path) + ["--offline"])
    assert config.offline is True


def test_invalid_mode_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_config(make_args(tmp_path, **{"--mode": "delete"}))
    assert error.value.code == 2


def test_missing_root_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_config(make_args(tmp_path / "does-not-exist"))
    assert error.value.code == 2


def test_root_that_is_a_file_is_rejected(tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("hello")
    with pytest.raises(SystemExit) as error:
        parse_config(make_args(a_file))
    assert error.value.code == 2


def test_empty_task_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_config(make_args(tmp_path, **{"--task": "   "}))
    assert error.value.code == 2


def test_missing_required_option_is_rejected(tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_config(["--root", str(tmp_path), "--task", "x", "--mode", "edit"])
    assert error.value.code == 2


def test_main_prints_summary(tmp_path, capsys):
    main(make_args(tmp_path) + ["--offline"])
    output = capsys.readouterr().out

    assert "=== Task ===" in output
    assert str(tmp_path.resolve()) in output
    assert "Fix the incorrect total" in output
    assert "readonly" in output
    assert "llama3.1" in output
    assert "Offline: yes" in output


def test_check_defaults_to_unittest(tmp_path):
    config = parse_config(make_args(tmp_path))
    assert config.check_command == "unittest"
    assert config.final_checks == ("unittest",)


def test_check_can_select_pytest(tmp_path):
    config = parse_config(make_args(tmp_path) + ["--check", "pytest"])
    assert config.check_command == "pytest"
    assert config.final_checks == ("pytest",)


def test_check_must_be_whitelisted(tmp_path):
    with pytest.raises(SystemExit) as error:
        parse_config(make_args(tmp_path) + ["--check", "bash"])
    assert error.value.code == 2


@pytest.mark.parametrize("dangerous_root", ["filesystem root", "home", "harness parent"])
def test_dangerous_roots_are_rejected(dangerous_root):
    from pathlib import Path
    import main as main_module
    root = {
        "filesystem root": Path(Path.cwd().anchor),
        "home": Path.home(),
        "harness parent": main_module.HARNESS_DIR.parent,
    }[dangerous_root]
    with pytest.raises(SystemExit) as error:
        parse_config(make_args(root))
    assert error.value.code == 2


def test_selected_files_are_stored(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    config = parse_config(make_args(tmp_path) + ["--file", "a.py", "--file", "./a.py"])
    assert config.context_files == ("a.py", "./a.py")


def test_no_files_selected_by_default(tmp_path):
    assert parse_config(make_args(tmp_path)).context_files == ()


@pytest.mark.parametrize("bad_file", ["missing.py", "../outside.txt", "sub"])
def test_bad_selected_file_is_rejected(tmp_path, bad_file):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("secret\n")
    with pytest.raises(SystemExit) as error:
        parse_config(make_args(root) + ["--file", bad_file])
    assert error.value.code == 2


def test_selected_file_is_sent_to_the_model(tmp_path, capsys, monkeypatch):
    import main as main_module
    from model_client import ScriptedModelClient

    (tmp_path / "a.py").write_text("SELECTED_CONTENT = 1\n")
    model = ScriptedModelClient([{"tool": "done", "arguments": {}}])
    monkeypatch.setattr(main_module, "build_model", lambda config: model)

    main(make_args(tmp_path) + ["--file", "a.py"])

    assert "SELECTED_CONTENT = 1" in model.received[0][1]["content"]
    assert "Files:   a.py" in capsys.readouterr().out
