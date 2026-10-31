"""Command-line parsing and presentation; task execution lives in application.py."""

import argparse
import json
import sys
from pathlib import Path

import application
from config import MODES, SANDBOXES, Config, load_settings
from execution import ALLOWED_COMMANDS, ExecutionError
from projects import HARNESS_DIR, load_projects, validate_root, require_external
from repository_tools import RepositoryError
from session import SessionError, SessionStore
from task_record import ScopeError
from controller import DONE

def build_legacy_parser():
    """Create the argparse parser that describes the command-line options."""
    parser = argparse.ArgumentParser(
        description="A small coding harness that works on a target repository.",
    )
    parser.add_argument(
        "--root",
        required=True,
        help="Path to the target repository.",
    )
    parser.add_argument('--projects-file', metavar='FILE',
                        help='Apply explicit project approvals to the Stage 1 launch form.')
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
    parser = build_legacy_parser()
    args = parser.parse_args(argv)
    config = config_from_args(args, parser)
    projects_file = args.projects_file or (DEFAULT_PROJECTS_FILE if DEFAULT_PROJECTS_FILE.exists() else None)
    if projects_file:
        try:
            projects = load_projects(projects_file)
            if config.root not in projects.values():
                raise ValueError(f'project {config.root} is not in the allowed projects list')
            if args.settings:
                require_external(args.settings, projects.values(), 'settings file')
            config.allowed_projects = tuple(str(root) for root in projects.values())
            config.projects_file = str(projects_file)
        except ValueError as error:
            parser.error(str(error))
    return config


def config_from_args(args, parser):
    """Shared validation for Stage 1 arguments and the approved start command."""
    root = Path(args.root).expanduser().resolve()
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
        projects_file=str(getattr(args, 'projects_file', '') or ''),
        check_command=args.check,
        final_checks=(args.check,),
        **settings,
    )


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




DEFAULT_PROJECTS_FILE = Path.home() / '.harness' / 'projects.toml'
DEFAULT_SESSION_DIR = Path.home() / '.harness' / 'sessions'


def build_parser():
    parser = argparse.ArgumentParser(description='Start and review coding tasks in approved repositories.')
    common = argparse.ArgumentParser(add_help=False)
    # Suppressed child defaults allow these options before OR after a command.
    for target in (parser, common):
        target.add_argument('--projects-file', default=argparse.SUPPRESS, metavar='FILE',
                            help=f'Approved projects TOML (default: {DEFAULT_PROJECTS_FILE}).')
        target.add_argument('--session-dir', default=argparse.SUPPRESS, metavar='DIR',
                            help=f'Saved tasks (default: {DEFAULT_SESSION_DIR}).')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('projects', parents=[common], help='List explicitly approved repositories.')
    commands.add_parser('list', parents=[common], help='List saved tasks.')
    start = commands.add_parser('start', parents=[common], help='Start a task in an approved repository.')
    start.add_argument('--project', required=True, help='Approved name from the projects TOML file.')
    start.add_argument('--task', required=True, help='Request to carry out in the selected project.')
    start.add_argument('--mode', choices=MODES, required=True,
                       help='readonly: inspect; edit: allow changes within the task scope.')
    start.add_argument('--model', required=True, help='Ollama model name.')
    start.add_argument('--offline', action='store_true', help='Use scripted replies; checks still run.')
    start.add_argument('--file', dest='files', action='append', default=[], metavar='PATH',
                       help='Relative file to include as context; repeat for several.')
    start.add_argument('--allow', dest='allowed_changes', action='append', default=[], metavar='GLOB',
                       help='Relative file/glob the agent may edit; repeat. Default: any project file.')
    start.add_argument('--acceptance', action='append', default=[], metavar='PATH',
                       help='External acceptance check to save with the task; repeat for several.')
    start.add_argument('--record', metavar='FILE', help='Also save a JSON task record outside the project.')
    start.add_argument('--check', choices=sorted(ALLOWED_COMMANDS), default='unittest',
                       help='Whitelisted check for the loop and final verification (default: unittest).')
    start.add_argument('--sandbox', choices=SANDBOXES,
                       help='docker (default): contained; none: run checks on this machine.')
    start.add_argument('--settings', metavar='FILE', help='TOML limits, timeouts and sandbox settings.')
    for name, help_text in (
            ('status', 'Display saved progress without running tools or tests.'),
            ('show', 'Display saved history, results, checks, and diff.'),
            ('cancel', 'Request cooperative cancellation from another terminal.'),
            ('resume', 'Revalidate and safely resume a saved task.')):
        command = commands.add_parser(name, parents=[common], help=help_text)
        command.add_argument('id', help='Saved task id (see list).')
        if name == 'cancel':
            command.add_argument('--reason', default='cancelled by the user')
        if name == 'show':
            command.add_argument('--live-diff', action='store_true',
                                 help='Also inspect the current approved repository diff; runs no tests.')
        if name == 'resume':
            command.add_argument('--model', help='Override the saved model name.')
            command.add_argument('--offline', action='store_true')
            command.add_argument('--sandbox', choices=SANDBOXES)
            command.add_argument('--dry-run', action='store_true',
                                 help='Check safety; execute no actions and leave the checkpoint unchanged.')
    return parser


def approved_projects(args):
    return load_projects(args.projects_file)


def approved_root(saved, projects):
    root = Path(saved.spec.root).resolve()
    if root not in projects.values():
        raise ValueError(f'project {root} is not in the allowed projects list; '
                         'approve it in the projects configuration before continuing')
    return root


def show_task(saved):
    """Render persisted data: inspection works even if the repository is gone."""
    section('Task', saved.spec.summary())
    section('Progress', saved.progress())
    section('History')
    if not saved.actions:
        print('(no recorded actions)')
    for action in saved.actions:
        print(action.text())
        if action.detail:
            print(action.detail)
    section('Conversation history')
    print(json.dumps(saved.checkpoint.messages, indent=2, ensure_ascii=False))
    result = saved.result
    section('Results', f'Status: {saved.status}\n{saved.summary or "(no summary saved)"}\n'
            + (f'Verification: {result["verdict"]}\n'
               f'Result: {"SUCCESS" if result["exit_code"] == 0 else "FAILURE"}'
               if result else 'Verification: UNAVAILABLE (no final result saved)'))
    section('Checks', result.get('checks_text') or 'NOT RUN: no final checks saved')
    section('Changed Files', '\n'.join(result.get('changed_files', saved.checkpoint.changed_files))
            or '(no changes recorded)')
    section('Diff', (f'UNAVAILABLE: {result["git_problem"]}' if result.get('git_problem')
                     else result.get('diff', '').rstrip() or
                     ('(no changes)' if result else 'UNAVAILABLE: no final diff saved; use --live-diff')))


def report_run(config, outcome):
    run, verification = outcome.run, outcome.verification
    section('Actions', run.record.actions_text() if run.record else '(not recorded)')
    section('Changed Files', verification.changed_files_text())
    section('Checks', verification.checks_text())
    section('Diff', verification.diff_text())
    section('Summary', summary_text(run, verification))
    if config.record_path:
        print(f'\nTask record written to {config.record_path}')
    if run.session is not None:
        print(f'Session {run.session.id} saved in {config.session_dir}')
        print(f'Inspect it with: python main.py show {run.session.id} --session-dir {config.session_dir}')
    return outcome.exit_code


def start_notice(config, spec, session):
    section('Task', config.summary())
    section('Task Record', spec.summary())
    if session:
        print(f'Session: {session.id}', flush=True)
    section('Progress')


def report_resume(saved, report):
    section('Saved session', saved.progress())
    section('Resume check', report.text())


def run_resume(args, *, projects=None, model_factory, sandbox_factory):
    saved = SessionStore(args.session_dir).load(args.id)
    if projects is None and (saved.run_settings.get('approval_required') or
                             getattr(args, 'projects_file', None) or DEFAULT_PROJECTS_FILE.exists()):
        projects = load_projects(getattr(args, 'projects_file', None) or DEFAULT_PROJECTS_FILE)
    if projects is not None:
        approved_root(saved, projects)
    config = application.config_from_session(
        saved, args.session_dir, model=args.model, offline=args.offline,
        sandbox=args.sandbox,
        allowed_projects=tuple(projects.values()) if projects is not None else args.allowed_projects,
    )
    report, outcome = application.resume_task(
        args.id, config, dry_run=args.dry_run, model_factory=model_factory,
        sandbox_factory=sandbox_factory, log=print, on_report=report_resume)
    if not report.safe:
        return 3
    if args.dry_run:
        print('--dry-run: nothing was executed; checkpoint unchanged.')
        return 0
    return report_run(config, outcome)


def run_cancel(args):
    saved = application.cancel_task(args.id, args.session_dir, args.reason)
    if saved.finished:
        print(f'Session {saved.id} already finished with status {saved.status!r}; nothing was running.')
    else:
        print(f'Cancellation requested for {saved.id}: {args.reason}')
        print('The running process will stop at its next cancellation check.')
    return 0


def list_tasks(directory):
    sessions = SessionStore(directory).list_sessions()
    if not sessions:
        print(f'No sessions in {directory}')
    for saved in sessions:
        flag = '  UNCERTAIN ACTION' if saved.has_uncertain_action else ''
        print(f'{saved.id}  {saved.status:<22} {saved.spec.request[:60]}{flag}')
    return 0


def legacy(argv, *, model_factory, sandbox_factory):
    """The Stage 1 launch form and existing session flags remain available."""
    first = argv[0] if argv else ''
    if first in ('--resume', '--cancel', '--list-sessions'):
        parser = argparse.ArgumentParser(description='Stage 1 session compatibility interface')
        parser.add_argument(first, required=first != '--list-sessions',
                            **({'action': 'store_true'} if first == '--list-sessions' else {}))
        parser.add_argument('--session-dir', required=True)
        if first == '--cancel':
            parser.add_argument('--reason', default='cancelled by the user')
        if first == '--resume':
            parser.add_argument('--projects-file')
            parser.add_argument('--model')
            parser.add_argument('--offline', action='store_true')
            parser.add_argument('--sandbox', choices=SANDBOXES)
            parser.add_argument('--allowed-project', action='append', default=[], dest='allowed_projects')
            parser.add_argument('--dry-run', action='store_true')
        args = parser.parse_args(argv)
        if first == '--list-sessions':
            return list_tasks(args.session_dir)
        args.id = getattr(args, first[2:].replace('-', '_'))
        return (run_cancel(args) if first == '--cancel' else
                run_resume(args, model_factory=model_factory, sandbox_factory=sandbox_factory))
    config = parse_config(argv)
    outcome = application.start_task(config, model_factory=model_factory,
                                     sandbox_factory=sandbox_factory, log=print, on_start=start_notice)
    return report_run(config, outcome)


def main(argv=None, *, model_factory=None, sandbox_factory=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    model_factory = model_factory or application.build_model
    sandbox_factory = sandbox_factory or application.build_sandbox
    try:
        if argv and (argv[0] in ('--resume', '--cancel', '--list-sessions') or '--root' in argv):
            return legacy(argv, model_factory=model_factory, sandbox_factory=sandbox_factory)
        parser = build_parser()
        args = parser.parse_args(argv)
        args.projects_file = getattr(args, 'projects_file', str(DEFAULT_PROJECTS_FILE))
        args.session_dir = str(Path(getattr(args, 'session_dir', DEFAULT_SESSION_DIR)).expanduser().resolve())
        if args.command == 'projects':
            for name, root in approved_projects(args).items():
                print(f'{name}\t{root}')
            return 0
        if args.command == 'list':
            return list_tasks(args.session_dir)
        if args.command == 'start':
            projects = approved_projects(args)
            if args.project not in projects:
                parser.error(f'project {args.project!r} is not approved. Allowed: {", ".join(projects)}')
            args.root = projects[args.project]
            args.allowed_projects = tuple(str(root) for root in projects.values())
            if args.settings:
                require_external(args.settings, projects.values(), 'settings file')
            config = config_from_args(args, parser)
            outcome = application.start_task(config, project=args.project,
                                             model_factory=model_factory, sandbox_factory=sandbox_factory,
                                             log=print, on_start=start_notice)
            return report_run(config, outcome)
        if args.command == 'resume':
            return run_resume(args, projects=approved_projects(args), model_factory=model_factory,
                              sandbox_factory=sandbox_factory)
        if args.command == 'cancel':
            return run_cancel(args)
        saved = SessionStore(args.session_dir).load(args.id)
        if args.command == 'status':
            print(saved.progress())
        else:
            show_task(saved)
            if args.live_diff:
                root = approved_root(saved, approved_projects(args))
                from verification import Verifier
                live = Verifier(root, check_names=()).verify()
                section('Current working-copy diff (no tests run)', live.diff_text())
        return 0
    except (ValueError, OSError, SessionError, RepositoryError, ScopeError, ExecutionError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Interrupted by the user. No further actions were executed.', file=sys.stderr)
        return 130
