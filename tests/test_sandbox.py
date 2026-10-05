"""Tests for the Docker sandbox.

Most tests only look at the docker command the sandbox builds, or use a
fake sandbox, so they need no Docker. The tests at the end run real
containers and are skipped when Docker or the harness-sandbox image is
not available (build it with: docker build -t harness-sandbox -f sandbox/Dockerfile .).
"""

import os
import sys
import time

import pytest

import main
from execution import PYTHON, DockerSandbox, ExecutionEnvironment, ExecutionError
from model_client import ScriptedModelClient


def make_repo(tmp_path, test_code):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text(test_code)
    return root


# ----- the docker command ------------------------------------------------------

def test_wrap_isolates_the_command(tmp_path):
    args = DockerSandbox("my-image").wrap([PYTHON, "-m", "pytest"], tmp_path, "c1")

    assert args[:2] == ["docker", "run"]
    assert args[-4:] == ["my-image", "python", "-m", "pytest"]   # container's own python
    flags = " ".join(args)
    assert "--rm" in args and "--name c1" in flags
    assert "--network none" in flags                          # no network
    assert "--read-only" in args and "--cap-drop ALL" in flags
    assert f"type=bind,source={tmp_path},target=/work,readonly" in flags
    assert flags.count("--mount") == 1                        # only the repository


def test_extra_mounts_are_read_only(tmp_path):
    args = DockerSandbox(mounts=[(tmp_path, "/acceptance")]).wrap(["python"], tmp_path, "c1")

    assert f"type=bind,source={tmp_path},target=/acceptance,readonly" in args


def test_host_secrets_do_not_reach_the_container(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sk-secret")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    args = " ".join(DockerSandbox().wrap(["python"], tmp_path, "c1"))

    assert "sk-secret" not in args and "MY_API_KEY" not in args
    assert "--env PYTEST_ADDOPTS=-q" in args


# ----- missing Docker or image ---------------------------------------------------

def test_missing_docker_program_is_a_clear_error():
    with pytest.raises(ExecutionError, match="Docker is not installed"):
        DockerSandbox(docker="docker-program-that-does-not-exist").check()


@pytest.mark.skipif(os.name != "posix", reason="uses a shell-less script as fake docker")
def test_missing_image_explains_how_to_build_it(tmp_path):
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(f"#!{sys.executable}\nimport sys\n"
                           "sys.stderr.write('No such image: x\\n')\nsys.exit(1)\n")
    fake_docker.chmod(0o755)

    with pytest.raises(ExecutionError, match="docker build -t harness-sandbox"):
        DockerSandbox(docker=str(fake_docker)).check()


def test_harness_stops_before_the_model_when_sandbox_is_missing(tmp_path, monkeypatch, capsys):
    model = ScriptedModelClient([{"tool": "done", "arguments": {}}])
    monkeypatch.setattr(main, "build_model", lambda config: model)
    monkeypatch.setattr(main, "build_sandbox",
                        lambda config: DockerSandbox(docker="docker-program-that-does-not-exist"))

    exit_code = main.main(["--root", str(tmp_path), "--task", "x", "--mode", "edit",
                           "--model", "m"])

    assert exit_code == 2
    assert "Docker is not installed" in capsys.readouterr().err
    assert model.received == []   # the model was never asked


# ----- timeout with a sandbox ----------------------------------------------------

class FakeSandbox:
    """Runs a slow local command instead of docker and records stop() calls."""

    def __init__(self):
        self.stopped = []

    def describe(self):
        return "fake"

    def wrap(self, command, root, container_name):
        self.container = container_name
        return [PYTHON, "-c", "import time; time.sleep(30)"]

    def stop(self, container_name):
        self.stopped.append(container_name)


def test_timeout_removes_the_container(tmp_path):
    sandbox = FakeSandbox()
    root = make_repo(tmp_path, "")
    start = time.monotonic()
    result = ExecutionEnvironment(root, timeout_seconds=0.5, sandbox=sandbox).run("unittest")

    assert result.timed_out and time.monotonic() - start < 10
    assert sandbox.stopped == [sandbox.container]   # killing the client is not enough
    assert result.sandbox == "fake"
    assert "Sandbox: fake" in result.report()


def test_without_sandbox_the_report_says_so(tmp_path):
    root = make_repo(tmp_path, "import unittest\nclass T(unittest.TestCase):\n"
                               "    def test_ok(self): pass\n")
    result = ExecutionEnvironment(root).run("unittest")

    assert "Sandbox: none (ran directly on this machine)" in result.report()


# ----- real containers (skipped without Docker and the image) ------------------

def docker_ready():
    try:
        DockerSandbox().check()
    except ExecutionError:
        return False
    return True


needs_docker = pytest.mark.skipif(not docker_ready(),
                                  reason="Docker or the harness-sandbox image is not available")

PROBE_TEST = '''
import os, socket, unittest
from pathlib import Path

class TestProbe(unittest.TestCase):
    def test_probe(self):
        print("SECRET=" + str(os.environ.get("MY_API_KEY")))
        print("HOST_FILE=" + str(Path(r"{host_file}").exists()))
        try:
            socket.create_connection(("1.1.1.1", 53), timeout=3)
            print("NETWORK=open")
        except OSError:
            print("NETWORK=blocked")
        try:
            open("written.txt", "w").write("x")
            print("WRITE=allowed")
        except OSError:
            print("WRITE=blocked")
'''


@needs_docker
def test_container_cannot_see_secrets_host_files_or_network(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "sk-secret")
    host_file = tmp_path / "host_secret.txt"
    host_file.write_text("secret")
    root = make_repo(tmp_path, PROBE_TEST.format(host_file=host_file))

    result = ExecutionEnvironment(root, timeout_seconds=60, sandbox=DockerSandbox()).run("unittest")

    assert result.succeeded, result.report()
    assert "SECRET=None" in result.stdout
    assert "HOST_FILE=False" in result.stdout
    assert "NETWORK=blocked" in result.stdout
    assert "WRITE=blocked" in result.stdout
    assert not (root / "written.txt").exists()


@needs_docker
def test_failing_check_in_container_stays_visible(tmp_path):
    root = make_repo(tmp_path, "import unittest\nclass T(unittest.TestCase):\n"
                               "    def test_wrong(self): self.assertEqual(1, 2)\n")

    result = ExecutionEnvironment(root, timeout_seconds=60, sandbox=DockerSandbox()).run("unittest")

    assert result.exit_code == 1 and not result.succeeded
    assert "test_wrong" in result.stderr
