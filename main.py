"""Entry point for the coding harness CLI.

Example:
    python main.py --root ./target-repo --task "Fix the incorrect total" \
        --mode readonly --model llama3.1 --file src/orders.py

It parses and validates the arguments, builds the tools and the model
client, and hands everything to the Controller, which runs the loop.
Afterwards the Verifier checks the repository on its own, whatever the
model claimed, and the results are printed in sections.
"""

import argparse
import sys
from pathlib import Path

from config import MODES, SANDBOXES, Config, load_settings
from controller import DONE, Controller
from execution import ALLOWED_COMMANDS, DockerSandbox, ExecutionEnvironment, ExecutionError
from model_client import ModelClient, ScriptedModelClient
from navigation import RepositoryNavigator
from repository_tools import RepositoryTools
from task_record import ScopeError, spec_from_config
from verification import Verifier

# The folder holding the harness itself. The agent must never be able to edit it.
HARNESS_DIR = Path(__file__).resolve().parent

# Replies used by --offline: a tiny fixed "conversation" that needs no model.
OFFLINE_SCRIPT = [
    {"tool": "list_files", "arguments": {"path": "."}},
    {"tool": "done", "arguments": {},
     "summary": "Offline run: listed the files. No model was contacted."},
]


def build_parser():
    """Create the argparse parser that describes the command-line options."""
    parser = argparse.ArgumentParser(
        description="A small coding harness that works on a target repository.",
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Path to the target repository.",
    )
    parser.add_argument(
        "--task",
        required=True,
        help="Coding task to perform, e.g. \"Fix the incorrect total\".",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=MODES,  # argparse rejects any other value for us
        help="readonly: only inspect files. edit: allow file changes.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Ollama model name, e.g. llama3.1.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Do not contact Ollama (for scripted/fake model testing).",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        dest="files",
        metavar="PATH",
        help="A file (relative to --root) to send to the model with the task. "
             "Repeat to select several files.",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        dest="allowed_changes",
        metavar="GLOB",
        help="A file (or glob, relative to --root) the agent may change, e.g. "
             "'service_layer/services.py' or 'domain/*.py'. Repeat for several. "
             "Without it, any file inside --root may be changed.",
    )
    parser.add_argument(
        "--acceptance",
        action="append",
        default=[],
        dest="acceptance",
        metavar="PATH",
        help="An acceptance check saved with the task. It must live OUTSIDE --root, "
             "so the agent cannot change what judges it. Repeat for several.",
    )
    parser.add_argument(
        "--record",
        metavar="FILE",
        help="Write the task specification and every action to this JSON file.",
    )
    parser.add_argument(
        "--check",
        default="unittest",
        choices=sorted(ALLOWED_COMMANDS),
        help="Which whitelisted test command run_check and verification use "
             "(default: unittest).",
    )
    parser.add_argument(
        "--sandbox",
        choices=SANDBOXES,
        default=None,  # None: use the settings file, else "docker"
        help="Where checks run. docker (default): a container with no network that only "
             "sees the repository. none: directly on this machine (NOT contained).",
    )
    parser.add_argument(
        "--settings",
        metavar="FILE",
        help="TOML file with limits, timeouts and sandbox settings "
             "(see settings.example.toml).",
    )
    return parser


def parse_config(argv=None):
    """Parse and validate command-line arguments and return a Config.

    argv is a list of strings. When it is None, argparse reads sys.argv,
    which is what happens in normal use. Tests pass their own list.

    On invalid input this prints an error and exits with status 2
    (argparse's standard behaviour), so the caller never gets a bad Config.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.exists():
        parser.error(f"--root path does not exist: {root}")
    if not root.is_dir():
        parser.error(f"--root must be a directory: {root}")
    if root == Path(root.anchor) or root == Path.home().resolve():
        parser.error(f"--root must be a project folder, not {root}")
    if HARNESS_DIR.is_relative_to(root):
        parser.error(f"--root must not contain the harness itself ({HARNESS_DIR})")

    task = args.task.strip()
    if not task:
        parser.error("--task must not be empty")

    model = args.model.strip()
    if not model:
        parser.error("--model must not be empty")

    for path in args.files:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            parser.error(f"--file must be inside --root: {path}")
        if not target.is_file():
            parser.error(f"--file is not a file: {path}")

    settings = {}
    if args.settings:
        try:
            settings = load_settings(args.settings)
        except ValueError as error:
            parser.error(str(error))
    if args.sandbox:  # the command line wins over the settings file
        settings["sandbox"] = args.sandbox

    # An acceptance check inside --root could be edited by the agent itself.
    for path in args.acceptance:
        if not Path(path).exists():
            parser.error(f"--acceptance path does not exist: {path}")
        if Path(path).resolve().is_relative_to(root):
            parser.error(f"--acceptance must be outside --root so the agent cannot "
                         f"change what judges it: {path}")

    return Config(
        root=root,
        task=task,
        mode=args.mode,
        model=model,
        offline=args.offline,
        context_files=tuple(args.files),
        allowed_changes=tuple(args.allowed_changes),
        acceptance_commands=tuple(args.acceptance),
        record_path=args.record or "",
        check_command=args.check,
        final_checks=(args.check,),
        **settings,
    )


def build_model(config):
    """Return the real Ollama client, or a scripted one with --offline."""
    if config.offline:
        return ScriptedModelClient(OFFLINE_SCRIPT)
    return ModelClient(config.ollama_settings())


def build_sandbox(config):
    """Return the sandbox checks run in, or None for --sandbox none."""
    if config.sandbox == "none":
        return None
    return DockerSandbox(config.sandbox_image)


def section(title, body=None):
    """Print a section header like '=== Checks ===' and optional text."""
    print(f"\n=== {title} ===")
    if body is not None:
        print(body)


def summary_text(run, verification):
    """The final Summary section: what the model said vs what was verified."""
    if run.status == DONE:
        model_says = run.summary
    else:
        model_says = f"(the model did not finish) {run.summary}"
    success = run.status == DONE and verification.passed
    return "\n".join([
        f"Run:          {run.status} ({run.counters_text()})",
        f"Stopped:      {run.stopping_rule()}",
        f"Model says:   {model_says}",
        f"Verification: {verification.verdict()}",
        f"Result:       {'SUCCESS' if success else 'FAILURE'}",
    ])


def main(argv=None):
    """Run the CLI. Returns the process exit code (0 = success)."""
    config = parse_config(argv)
    sandbox = build_sandbox(config)
    if sandbox:
        try:
            sandbox.check()  # fail now, not after the model has worked for minutes
        except ExecutionError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    tools = RepositoryTools(config.root, config.mode)
    navigator = RepositoryNavigator(tools, timeout_seconds=config.command_timeout_seconds)

    # Pin the task to the commit the repository is on right now.
    try:
        spec = spec_from_config(config, starting_commit=navigator.head_commit(),
                                acceptance_commands=config.acceptance_commands)
    except ScopeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    section("Task", config.summary())
    section("Task Record", spec.summary())

    controller = Controller(
        config=config,
        model=build_model(config),
        tools=tools,
        executor=ExecutionEnvironment(config.root, config.command_timeout_seconds,
                                      sandbox=sandbox),
        navigator=navigator,
        spec=spec,
    )
    section("Progress")
    run = controller.run(config.task, config.context_files)

    # Verification runs no matter what the model said.
    verifier = Verifier(config.root, config.final_checks, config.command_timeout_seconds,
                        sandbox=sandbox)
    verification = verifier.verify()

    section("Actions", run.record.actions_text() if run.record else "(not recorded)")
    section("Changed Files", verification.changed_files_text())
    section("Checks", verification.checks_text())
    section("Diff", verification.diff_text())
    section("Summary", summary_text(run, verification))

    if config.record_path and run.record:
        saved = run.record.save(config.record_path)
        print(f"\nTask record written to {saved}")

    return 0 if run.status == DONE and verification.passed else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Running commands were already stopped by ExecutionEnvironment.
        print("\nInterrupted by the user. No further actions were executed.")
        sys.exit(130)
