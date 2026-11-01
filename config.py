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
#   none:   repository checks are unavailable; inspection/editing still works.
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
# Longest reply the model may generate for one action, in tokens. One action is
# a short JSON object, so this is generous. Without it a model that starts
# rewriting a whole file keeps generating until the context window is full,
# which surfaces only as a request timeout; a cut-off reply is instead reported
# to the model as a malformed one and counted against max_retries.
MAX_REPLY_TOKENS = 1200

# Context window (tokens) requested from Ollama. Ollama's own default is small
# (a few thousand tokens) and it silently drops the oldest messages when the
# conversation is longer, so the model would forget the system prompt and task.
DEFAULT_NUM_CTX = 16384

# ----- context management (see context.py) -----
# The documented size limit for the whole conversation sent to the model, in
# characters. A local model needs roughly 3-4 characters per token, so this sits
# well inside DEFAULT_NUM_CTX and leaves room for the reply. The full history is
# still saved with the session: the budget limits what is sent, not what is kept.
CONTEXT_BUDGET_CHARS = 24000
# Newest turns (one model reply plus its result) kept word for word; older steps
# are shortened to one line each.
CONTEXT_KEEP_TURNS = 3


@dataclass
class OllamaSettings:
    """How to reach the language model. Used only by ModelClient."""

    model: str
    base_url: str = DEFAULT_OLLAMA_URL
    timeout_seconds: int = 300
    temperature: float = 0.0  # 0 = as predictable as possible
    num_ctx: int = DEFAULT_NUM_CTX
    max_reply_tokens: int = MAX_REPLY_TOKENS


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

    # Glob patterns (relative to root) the agent may change. () = any file in
    # the root, which is the Stage 1 behaviour. Enforced by the controller
    # before a write happens, not checked afterwards.
    allowed_changes: tuple = ()

    # Acceptance checks saved with the task. They live OUTSIDE root, so the
    # agent cannot change what judges it; main.py refuses anything inside root.
    acceptance_commands: tuple = ()

    # Where to write the task record (specification + every action). None = nowhere.
    record_path: str = ""

    # Where sessions are saved, so a run can be reopened and resumed. "" = not saved.
    session_dir: str = ""

    # Projects the harness may work on at all. () = no allowlist (any --root that
    # passes the safety checks). Revalidated before a session is resumed.
    allowed_projects: tuple = ()

    # Trusted approval configuration used by the CLI; empty for Stage 1 callers.
    projects_file: str = ""

    ollama_url: str = DEFAULT_OLLAMA_URL
    model_timeout_seconds: int = 300  # local models can be slow, esp. the first call
    num_ctx: int = DEFAULT_NUM_CTX
    command_timeout_seconds: int = 30
    check_command: str = "unittest"  # what run_check runs when no name is given
    final_checks: tuple = ("unittest",)  # what verification runs at the end
    lint_checks: tuple = ()
    build_checks: tuple = ()

    max_actions: int = MAX_ACTIONS
    max_retries: int = MAX_RETRIES
    max_output_chars: int = MAX_OUTPUT_CHARS
    max_reply_tokens: int = MAX_REPLY_TOKENS

    # Context management. The budget bounds the conversation sent to the model;
    # context_map adds the repository map and the suggested files to the task.
    context_budget_chars: int = CONTEXT_BUDGET_CHARS
    context_keep_turns: int = CONTEXT_KEEP_TURNS
    context_map: bool = True

    sandbox: str = "docker"
    sandbox_image: str = DEFAULT_SANDBOX_IMAGE

    def ollama_settings(self):
        """Return just the settings ModelClient needs."""
        return OllamaSettings(
            model=self.model,
            base_url=self.ollama_url,
            timeout_seconds=self.model_timeout_seconds,
            num_ctx=self.num_ctx,
            max_reply_tokens=self.max_reply_tokens,
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
            f"Allowed: {', '.join(self.allowed_changes) or 'any file in the repository'}",
            f"Session: {self.session_dir or '(not saved)'}",
            f"Sandbox: {self.sandbox_text()}",
            f"Context: {self.context_text()}",
        ]
        return "\n".join(lines)

    def context_text(self):
        """How the conversation sent to the model is bounded (see context.py)."""
        parts = [f"budget {self.context_budget_chars} characters",
                 f"last {self.context_keep_turns} turns verbatim"]
        parts.append("repository map on" if self.context_map else "repository map off")
        return ", ".join(parts)

    def sandbox_text(self):
        if self.sandbox == "none":
            return "none (NOT contained; repository checks are unavailable)"
        return f"{self.sandbox} (image {self.sandbox_image})"


# Settings a settings file may change (see settings.example.toml).
# Everything else comes from the command line.
SETTINGS_KEYS = ("ollama_url", "model_timeout_seconds", "num_ctx", "command_timeout_seconds",
                 "max_actions", "max_retries", "max_output_chars", "max_reply_tokens",
                 "sandbox", "sandbox_image",
                 "lint_checks", "build_checks",
                 "context_budget_chars", "context_keep_turns", "context_map")


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
        if key in ("lint_checks", "build_checks"):
            allowed = ("ruff",) if key == "lint_checks" else ("compileall",)
            if not isinstance(value, list) or any(not isinstance(n, str) or n not in allowed for n in value):
                raise ValueError(f"setting {key!r} must be an array of check names: {', '.join(allowed)}")
            data[key] = tuple(dict.fromkeys(value))
        elif expected is bool:
            if not isinstance(value, bool):
                raise ValueError(f"setting {key!r} must be true or false")
        elif expected is int:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"setting {key!r} must be a whole number above 0")
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"setting {key!r} must be a non-empty text")
    if "sandbox" in data and data["sandbox"] not in SANDBOXES:
        raise ValueError(f"setting 'sandbox' must be one of {', '.join(SANDBOXES)}")
    return data
