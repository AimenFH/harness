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
import signal
import sys
from pathlib import Path

from cancellation import AnyCancel, CancelFile, CancelToken
from config import (MAX_ACTIONS, MAX_OUTPUT_CHARS, MAX_RETRIES, MODES, SANDBOXES,
                    Config, load_settings)
from controller import DONE, Controller
from execution import ALLOWED_COMMANDS, DockerSandbox, ExecutionEnvironment, ExecutionError
from model_client import ModelClient, ScriptedModelClient
from navigation import RepositoryNavigator
from repository_tools import RepositoryError, RepositoryTools
from session import (FINISHED, Session, SessionError, SessionStore, check_resume,
                     new_session_id)
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
        "--session-dir",
        metavar="DIR",
        dest="session_dir",
        help="Save the run as a session in this folder, so it can be reopened "
             "and resumed after a restart (see --resume and --list-sessions).",
    )
    parser.add_argument(
        "--allowed-project",
        action="append",
        default=[],
        dest="allowed_projects",
        metavar="PATH",
        help="A project the harness may work on. Repeat for several. Without it "
             "any --root that passes the safety checks is accepted. The list is "
             "revalidated before a session is resumed.",
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

    if args.allowed_projects and not any(
            root == Path(allowed).resolve() for allowed in args.allowed_projects):
        parser.error(f"--root {root} is not in the allowed projects: "
                     + ", ".join(args.allowed_projects))

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
        session_dir=args.session_dir or "",
        allowed_projects=tuple(args.allowed_projects),
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


def cancel_parser():
    parser = argparse.ArgumentParser(
        prog="main.py --cancel",
        description="Cancel a running task from another process.")
    parser.add_argument("--cancel", required=True, metavar="ID",
                        help="Session id of the run to cancel.")
    parser.add_argument("--session-dir", required=True, metavar="DIR", dest="session_dir")
    parser.add_argument("--reason", default="cancelled by the user",
                        help="Why it is being cancelled; shown in the run's output.")
    return parser


def cancel(argv):
    """Ask a running task to stop, from a second CLI process.

    This writes a small file next to the session. The running process notices
    it within a fraction of a second -- between steps, while waiting for the
    model, or while a command is running -- stops, cleans up, and saves its
    state. Cancelling the same task twice is harmless, and so is cancelling one
    that has already finished.
    """
    args = cancel_parser().parse_args(argv)
    store = SessionStore(args.session_dir)
    try:
        saved = store.load(args.cancel)
    except SessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    path = CancelFile(args.session_dir, args.cancel).request(args.reason)
    print(f"Cancellation requested for {args.cancel}: {args.reason}")
    print(f"Flag written to {path}")
    if saved.status in FINISHED:
        print(f"Note: that session already finished with status {saved.status!r}; "
              "nothing was running.")
    else:
        print("The running process will stop at its next check "
              "(between steps, or while waiting for the model or a command).")
    return 0


def resume_parser():
    """Options for reopening a saved session instead of starting a new task."""
    parser = argparse.ArgumentParser(
        prog="main.py --resume",
        description="Resume a saved session, after revalidating the repository.",
    )
    parser.add_argument("--resume", required=True, metavar="ID",
                        help="Session id to resume (see --list-sessions).")
    parser.add_argument("--session-dir", required=True, metavar="DIR", dest="session_dir",
                        help="Folder the sessions were saved in.")
    parser.add_argument("--model", help="Override the model name (default: the saved one).")
    parser.add_argument("--offline", action="store_true",
                        help="Do not contact Ollama (scripted replies).")
    parser.add_argument("--sandbox", choices=SANDBOXES, default=None)
    parser.add_argument("--allowed-project", action="append", default=[],
                        dest="allowed_projects", metavar="PATH",
                        help="A project the harness may work on; revalidated here.")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Only check whether resuming is safe; execute nothing.")
    return parser


def list_sessions_parser():
    parser = argparse.ArgumentParser(prog="main.py --list-sessions")
    parser.add_argument("--list-sessions", action="store_true")
    parser.add_argument("--session-dir", required=True, metavar="DIR", dest="session_dir")
    return parser


def list_sessions(argv):
    """Print every saved session, newest first."""
    args = list_sessions_parser().parse_args(argv)
    sessions = SessionStore(args.session_dir).list_sessions()
    if not sessions:
        print(f"No sessions in {args.session_dir}")
        return 0
    for saved in sessions:
        flag = "  UNCERTAIN ACTION" if saved.has_uncertain_action else ""
        print(f"{saved.id}  {saved.status:<22} {saved.spec.request[:60]}{flag}")
    return 0


def config_from_session(session, args):
    """Rebuild a Config from a saved session, with the few CLI overrides."""
    limits = session.spec.limits
    settings = {}
    if args.sandbox:
        settings["sandbox"] = args.sandbox
    return Config(
        root=Path(session.spec.root),
        task=session.spec.request,
        mode=session.spec.mode,
        model=(args.model or "resumed").strip(),
        offline=args.offline,
        allowed_changes=tuple(session.spec.allowed_changes),
        acceptance_commands=tuple(session.spec.acceptance_commands),
        session_dir=args.session_dir,
        allowed_projects=tuple(args.allowed_projects),
        check_command=session.spec.checks[0] if session.spec.checks else "unittest",
        final_checks=tuple(session.spec.checks) or ("unittest",),
        max_actions=limits.get("max_actions", MAX_ACTIONS),
        max_retries=limits.get("max_retries", MAX_RETRIES),
        max_output_chars=limits.get("max_output_chars", MAX_OUTPUT_CHARS),
        command_timeout_seconds=limits.get("command_timeout_seconds", 30),
        **settings,
    )


def resume(argv):
    """Reopen a session, revalidate everything, and carry on only if it is safe.

    Exit codes: 0 success, 1 the run did not finish cleanly, 2 the session
    could not be read, 3 PAUSED because the saved state no longer matches.
    """
    args = resume_parser().parse_args(argv)
    store = SessionStore(args.session_dir)
    try:
        session = store.reopen(args.resume)
    except SessionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    section("Saved session", session.progress())

    config = config_from_session(session, args)
    try:
        tools = RepositoryTools(config.root, config.mode)
    except RepositoryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    navigator = RepositoryNavigator(tools, timeout_seconds=config.command_timeout_seconds)
    branch, commit, dirty = navigator.state_summary()

    report = check_resume(session, config.root, branch, commit, dirty, config.mode,
                          config.allowed_changes, config.allowed_projects)
    section("Resume check", report.text())
    if not report.safe:
        return 3
    if args.dry_run:
        print("\n--dry-run: nothing was executed.")
        return 0

    # The flag that stopped this run last time must not stop it again at once.
    CancelFile(args.session_dir, session.id).clear()

    sandbox = build_sandbox(config)
    if sandbox:
        try:
            sandbox.check()
        except ExecutionError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2

    controller = Controller(
        config=config, model=build_model(config), tools=tools,
        executor=ExecutionEnvironment(config.root, config.command_timeout_seconds,
                                      sandbox=sandbox),
        navigator=navigator, spec=session.spec, session=session, store=store,
        cancel=build_cancel(config, session),
    )
    section("Progress")
    run = controller.resume(session)
    return report_run(config, run, sandbox)


def build_cancel(config, session):
    """The cancel sources for one run, and the SIGINT handler that uses them.

    Two sources, combined: a CancelToken for Ctrl+C in this terminal, and -- when
    the run is saved as a session -- a CancelFile another process can create with
    `--cancel <id>`. Ctrl+C is handled rather than left to raise, so the first
    press asks the run to stop cleanly (saving its state and cleaning up) and a
    second press still aborts the hard way.
    """
    token = CancelToken()
    sources = [token]
    if session is not None and config.session_dir:
        sources.append(CancelFile(config.session_dir, session.id))

    def on_interrupt(signum, frame):
        if token.cancelled:          # second Ctrl+C: give up waiting for a clean stop
            raise KeyboardInterrupt
        token.cancel("cancelled with Ctrl+C")
        print("\nCancelling: finishing the current step, then stopping. "
              "Press Ctrl+C again to abort immediately.", file=sys.stderr)

    try:
        signal.signal(signal.SIGINT, on_interrupt)
    except (ValueError, OSError):
        pass        # not the main thread, or no signals here: Ctrl+C just raises
    return AnyCancel(*sources)


def report_run(config, run, sandbox):
    """Verify the repository, print every section, and return the exit code.

    Shared by a fresh run and a resumed one, so both report the same way.
    Verification runs no matter what the model claimed.
    """
    verifier = Verifier(config.root, config.final_checks, config.command_timeout_seconds,
                        sandbox=sandbox)
    verification = verifier.verify()

    section("Actions", run.record.actions_text() if run.record else "(not recorded)")
    section("Changed Files", verification.changed_files_text())
    section("Checks", verification.checks_text())
    section("Diff", verification.diff_text())
    section("Summary", summary_text(run, verification))

    if config.record_path and run.record:
        print(f"\nTask record written to {run.record.save(config.record_path)}")
    if run.session is not None:
        print(f"Session {run.session.id} saved in {config.session_dir}")
        print(f"Resume it with: python main.py --resume {run.session.id} "
              f"--session-dir {config.session_dir}")

    return 0 if run.status == DONE and verification.passed else 1


def main(argv=None):
    """Run the CLI. Returns the process exit code (0 = success)."""
    argv = sys.argv[1:] if argv is None else list(argv)
    # Two alternative entry points that do not start a new task.
    if "--list-sessions" in argv:
        return list_sessions(argv)
    if "--cancel" in argv:
        return cancel(argv)
    if "--resume" in argv:
        return resume(argv)

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

    # With --session-dir the run is saved after every step and can be resumed.
    session = store = None
    if config.session_dir:
        session = Session(id=new_session_id(), spec=spec)
        store = SessionStore(config.session_dir)

    controller = Controller(
        config=config,
        model=build_model(config),
        tools=tools,
        executor=ExecutionEnvironment(config.root, config.command_timeout_seconds,
                                      sandbox=sandbox),
        navigator=navigator,
        spec=spec,
        session=session,
        store=store,
        cancel=build_cancel(config, session),
    )
    section("Progress")
    run = controller.run(config.task, config.context_files)
    return report_run(config, run, sandbox)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Running commands were already stopped by ExecutionEnvironment.
        print("\nInterrupted by the user. No further actions were executed.")
        sys.exit(130)
