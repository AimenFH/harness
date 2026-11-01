"""Task lifecycle services. No argument parsing or terminal presentation here."""

import signal
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path

from cancellation import AnyCancel, CancelFile, CancelToken
from config import MAX_ACTIONS, MAX_OUTPUT_CHARS, MAX_RETRIES, Config
from controller import CANCELLED, Controller
from execution import DockerSandbox, ExecutionEnvironment
from model_client import ModelClient, ScriptedModelClient
from navigation import RepositoryNavigator
from projects import require_external, validate_root
from repository_tools import RepositoryTools
from session import Checkpoint, Session, SessionStore, check_resume, new_session_id
from task_record import spec_from_config
from verification import Verifier

OFFLINE_SCRIPT = [
    {"tool": "list_files", "arguments": {"path": "."}},
    {"tool": "done", "arguments": {},
     "summary": "Offline run: listed the files. No model was contacted."},
]

# Saved independently of permission-bearing task fields. Resume restores these
# operational settings; permissions and limits always come from the TaskSpec.
RUN_SETTINGS = ("model", "ollama_url", "model_timeout_seconds", "num_ctx",
                "max_reply_tokens",
                "sandbox", "sandbox_image", "lint_checks", "build_checks",
                "context_budget_chars", "context_keep_turns", "context_map")


def build_model(config):
    return (ScriptedModelClient(OFFLINE_SCRIPT) if config.offline
            else ModelClient(config.ollama_settings()))


def build_sandbox(config):
    return None if config.sandbox == "none" else DockerSandbox(config.sandbox_image)


@contextmanager
def cancellation(config, session, notify):
    """Use cooperative cancellation and restore the caller's SIGINT handler."""
    token = CancelToken()
    sources = [token]
    if session is not None:
        sources.append(CancelFile(config.session_dir, session.id))

    def interrupt(signum, frame):
        if token.cancelled:
            raise KeyboardInterrupt
        token.cancel("cancelled with Ctrl+C")
        notify("Cancelling: finishing the current step, then stopping. "
               "Press Ctrl+C again to abort immediately.")

    previous = None
    try:
        previous = signal.signal(signal.SIGINT, interrupt)
    except (ValueError, OSError):
        pass  # Embedded callers may run outside the main thread.
    try:
        yield AnyCancel(*sources)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


@dataclass
class TaskOutcome:
    run: object
    verification: object

    @property
    def exit_code(self):
        if self.run.status == CANCELLED:
            return 130
        return 0 if self.verification.task_completed else 1


def validate_config(config):
    root = validate_root(config.root)
    if config.allowed_projects and root not in {
            Path(p).resolve() for p in config.allowed_projects}:
        raise ValueError(f"project {root} is not in the allowed projects list")
    roots = config.allowed_projects or (root,)
    if config.session_dir:
        require_external(config.session_dir, roots, "session directory")
    if config.record_path:
        require_external(config.record_path, roots, "task record")
    config.root = root


def config_from_session(session, session_dir, *, model=None, offline=False,
                        sandbox=None, allowed_projects=()):
    settings = {k: v for k, v in session.run_settings.items() if k in RUN_SETTINGS}
    settings.setdefault("model", "resumed")  # Older checkpoints did not save it.
    if model is not None:
        if not model.strip():
            raise ValueError("--model must not be empty")
        settings["model"] = model.strip()
    if sandbox is not None:
        settings["sandbox"] = sandbox
    limits = session.spec.limits
    return Config(
        root=Path(session.spec.root), task=session.spec.request, mode=session.spec.mode,
        offline=offline, session_dir=str(session_dir),
        allowed_changes=tuple(session.spec.allowed_changes),
        acceptance_commands=tuple(session.spec.acceptance_commands),
        allowed_projects=tuple(allowed_projects),
        check_command=session.spec.checks[0] if session.spec.checks else "unittest",
        final_checks=tuple(session.spec.checks) or ("unittest",),
        max_actions=limits.get("max_actions", MAX_ACTIONS),
        max_retries=limits.get("max_retries", MAX_RETRIES),
        max_output_chars=limits.get("max_output_chars", MAX_OUTPUT_CHARS),
        command_timeout_seconds=limits.get("command_timeout_seconds", 30), **settings,
    )


def resume_report(session, config):
    validate_config(config)
    tools = RepositoryTools(config.root, config.mode)
    navigator = RepositoryNavigator(tools, timeout_seconds=config.command_timeout_seconds)
    branch, commit, dirty = navigator.state_summary()
    report = check_resume(session, config.root, branch, commit, dirty, config.mode,
                          config.allowed_changes, config.allowed_projects)
    if session.has_uncertain_action:
        report.mismatches.append("an interrupted action has an uncertain outcome; inspect "
                                 "the history and diff, then start a new task")
    if not session.checkpoint.commit or not commit or not branch:
        report.mismatches.append("Git state is unavailable; a saved and current commit are "
                                 "required to validate a resume")
    return report


def start_task(config, *, project="", model_factory=build_model,
               sandbox_factory=build_sandbox, log=lambda message: None,
               on_start=lambda config, spec, session: None):
    validate_config(config)
    tools = RepositoryTools(config.root, config.mode)
    navigator = RepositoryNavigator(tools, timeout_seconds=config.command_timeout_seconds)
    spec = spec_from_config(config, project=project, starting_commit=navigator.head_commit(),
                            acceptance_commands=config.acceptance_commands)
    if project and not spec.starting_commit:
        raise ValueError("an approved project must be a Git repository with at least one commit")
    session = store = None
    if config.session_dir:
        session = Session(id=new_session_id(), spec=spec,
                          run_settings={key: getattr(config, key) for key in RUN_SETTINGS})
        session.run_settings["approval_required"] = bool(project or config.projects_file)
        branch, commit, dirty = navigator.state_summary()
        session.checkpoint = Checkpoint(branch=branch, commit=commit, dirty=dirty)
        store = SessionStore(config.session_dir)
    claim = store.claim(session.id) if store else nullcontext()
    with claim:
        sandbox = sandbox_factory(config)
        if sandbox:
            sandbox.check()
        if store:
            store.save(session)  # Addressable even during the first model request.
        on_start(config, spec, session)
        return _execute(config, tools, navigator, session, store, sandbox,
                        model_factory, log, spec=spec)


def resume_task(session_id, config, *, dry_run=False, model_factory=build_model,
                sandbox_factory=build_sandbox, log=lambda message: None,
                on_report=lambda session, report: None):
    store = SessionStore(config.session_dir)
    # Lock before loading: a concurrent runner must not overwrite the checkpoint
    # between validation and continuation. OS locks release even on a hard kill.
    with store.claim(session_id):
        session = store.reopen(session_id)
        report = resume_report(session, config)
        on_report(session, report)
        if not report.safe or dry_run:
            return report, None
        sandbox = sandbox_factory(config)
        if sandbox:
            sandbox.check()
        CancelFile(config.session_dir, session.id).clear()
        session.status = "running"
        session.result = {}
        session.run_settings.update({key: getattr(config, key) for key in RUN_SETTINGS})
        store.save(session)
        tools = RepositoryTools(config.root, config.mode)
        navigator = RepositoryNavigator(tools, timeout_seconds=config.command_timeout_seconds)
        outcome = _execute(config, tools, navigator, session, store, sandbox,
                           model_factory, log, resuming=True)
        return report, outcome


def _execute(config, tools, navigator, session, store, sandbox, model_factory, log,
             resuming=False, spec=None):
    with cancellation(config, session, log) as cancel:
        controller = Controller(
            config=config, model=model_factory(config), tools=tools,
            executor=ExecutionEnvironment(config.root, config.command_timeout_seconds,
                                          sandbox=sandbox),
            navigator=navigator, spec=session.spec if session else spec,
            session=session, store=store, cancel=cancel, log=log,
        )
        run = (controller.resume(session) if resuming
               else controller.run(config.task, config.context_files))
        if store:
            session.status = "verifying"
            store.save(session)  # Final checks are still part of the active task.
        final_checks = tuple(dict.fromkeys(config.final_checks + tuple(config.lint_checks) + tuple(config.build_checks)))
        verification = Verifier(config.root, final_checks,
                                config.command_timeout_seconds, sandbox=sandbox).verify(cancel=cancel)
        if cancel.cancelled:
            run.status = CANCELLED
            run.summary = f"Stopped: {cancel.reason}. No further actions were executed."
            if run.record:
                run.record.finish(CANCELLED, run.summary)
            if session:
                session.status = CANCELLED
                session.summary = run.summary
        elif session:
            session.status = run.status
        verification.assess_task(run, controller.spec)
    outcome = TaskOutcome(run, verification)
    if store:
        session.result = {
            "exit_code": outcome.exit_code, "verdict": verification.verdict(),
            "checks": [asdict(check) for check in verification.checks],
            "checks_text": verification.checks_text(),
            "changed_files": verification.changed_files, "diff": verification.diff,
            "git_problem": verification.git_problem,
            "tests_text": verification.checks_text('tests'),
            "lint_text": verification.checks_text('lint'),
            "build_text": verification.checks_text('build'),
            "diff_text": verification.diff_text(),
            "task_status": verification.task_status,
            "unfinished_work": verification.unfinished_work,
            "remaining_problems": verification.problems,
            "unfinished_text": verification.unfinished_text(),
        }
        store.save(session)
    if config.record_path and run.record:
        run.record.save(config.record_path)
    return outcome


def cancel_task(session_id, session_dir, reason):
    saved = SessionStore(session_dir).load(session_id)
    if not saved.finished:
        CancelFile(session_dir, session_id).request(reason)
    return saved
