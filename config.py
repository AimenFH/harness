"""Configuration: all settings for the harness in one place.

Keeping settings here means other modules do not hard-code values.
"""

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

# The two ways the harness may operate on a repository.
#   readonly: the model may list, read and search files and run checks; edit_file is denied.
#   edit:     the model may also change files inside the repository root.
MODES = ("readonly", "edit")

# Where repository code (checks and tests) runs.
#   docker: inside a throwaway container with no network and no host files.
#   none:   directly on this machine. NOT contained; only for when Docker is unavailable.
SANDBOXES = ("docker", "none")
DEFAULT_SANDBOX_IMAGE = "harness-sandbox"

DEFAULT_OLLAMA_URL = "http://localhost:11434"

# ----- safety / resource limits -----
# Every reply from the model counts as one action (valid, malformed or denied).
MAX_ACTIONS = 20
# Stop after this many rejected requests (malformed or denied) in a row.
MAX_RETRIES = 3
# Tool output longer than this is cut and marked with [OUTPUT TRUNCATED].
MAX_OUTPUT_CHARS = 10000

# Context window (tokens) requested from Ollama. Ollama's own default is small
# (a few thousand tokens) and it silently drops the oldest messages when the
# conversation is longer, so the model would forget the system prompt and task.
DEFAULT_NUM_CTX = 16384


@dataclass
class OllamaSettings:
    """How to reach the language model. Used only by ModelClient."""

    model: str
    base_url: str = DEFAULT_OLLAMA_URL
    timeout_seconds: int = 300
    temperature: float = 0.0  # 0 = as predictable as possible
    num_ctx: int = DEFAULT_NUM_CTX


@dataclass
class Config:
    """Settings for one run of the harness.

    root, task, mode, model, offline, context_files and the check
    (check_command and final_checks) come from the command line. The rest
    are defaults.
    """

    root: Path
    task: str
    mode: str
    model: str
    offline: bool = False
    context_files: tuple = ()  # files the user selected to send with the task

    ollama_url: str = DEFAULT_OLLAMA_URL
    model_timeout_seconds: int = 300  # local models can be slow, esp. the first call
    num_ctx: int = DEFAULT_NUM_CTX
    command_timeout_seconds: int = 30
    check_command: str = "unittest"  # what run_check runs when no name is given
    final_checks: tuple = ("unittest",)  # what verification runs at the end

    max_actions: int = MAX_ACTIONS
    max_retries: int = MAX_RETRIES
    max_output_chars: int = MAX_OUTPUT_CHARS

    sandbox: str = "docker"
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE

    def ollama_settings(self):
        """Return just the settings ModelClient needs."""
        return OllamaSettings(
            model=self.model,
            base_url=self.ollama_url,
            timeout_seconds=self.model_timeout_seconds,
            num_ctx=self.num_ctx,
        )

    def summary(self):
        """Return a human-readable, multi-line description of the settings."""
        lines = [
            f"Task:    {self.task}",
            f"Root:    {self.root}",
            f"Mode:    {self.mode}",
            f"Model:   {self.model}",
            f"Offline: {'yes' if self.offline else 'no'}",
            f"Files:   {', '.join(self.context_files) or '(none selected)'}",
            f"Sandbox: {self.sandbox_text()}",
        ]
        return "\n".join(lines)

    def sandbox_text(self):
        if self.sandbox == "none":
            return "none (checks run directly on this machine, NOT contained)"
        return f"{self.sandbox} (image {self.sandbox_image})"


# Settings a settings file may change (see settings.example.toml).
# Everything else comes from the command line.
SETTINGS_KEYS = ("ollama_url", "model_timeout_seconds", "num_ctx", "command_timeout_seconds",
                 "max_actions", "max_retries", "max_output_chars", "sandbox", "sandbox_image")


def load_settings(path):
    """Read a TOML settings file and return {setting: value}.

    Raises ValueError with a readable message for a missing file, bad TOML,
    an unknown setting, or a value of the wrong type.
    """
    try:
        with open(path, "rb") as file:
            data = tomllib.load(file)
    except OSError as error:
        raise ValueError(f"cannot read settings file {path}: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"settings file {path} is not valid TOML: {error}") from None

    types = {f.name: f.type for f in fields(Config)}
    for key, value in data.items():
        if key not in SETTINGS_KEYS:
            raise ValueError(f"unknown setting {key!r} in {path}. "
                             f"Allowed: {', '.join(SETTINGS_KEYS)}")
        expected = types[key]
        if expected is int:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"setting {key!r} must be a whole number above 0")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"setting {key!r} must be a non-empty text")
    if "sandbox" in data and data["sandbox"] not in SANDBOXES:
        raise ValueError(f"setting 'sandbox' must be one of {', '.join(SANDBOXES)}")
    return data
