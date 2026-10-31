"""Model client: the only part of the harness that talks to the language model.

The model never touches files or runs commands. It only *requests* an
action as JSON, for example:

    {"tool": "read_file", "arguments": {"path": "orders/pricing.py"}}

This module sends the conversation to Ollama, checks that the reply is a
well-formed action, and returns it as an Action object. Deciding whether
and how to carry out the action is the controller's job.
"""

import copy
import json
import threading
from dataclasses import dataclass, field

import requests

from cancellation import CancelledError, wait_for
from config import OllamaSettings

# Tool name -> {argument name: required?}. All argument values are strings.
TOOLS = {
    "list_files": {"path": False},
    "read_file": {"path": True},
    "search": {"query": True, "path": False},
    "find_files": {"pattern": True, "path": False},
    "list_symbols": {"path": True},
    "find_symbol": {"name": True, "path": False},
    "module_dependencies": {"path": True},
    "git_state": {},
    "replace_in_file": {"path": True, "old": True, "new": True},
    "edit_file": {"path": True, "content": True},
    "run_check": {"name": False},
    "done": {"summary": False},
}


# Tools that change files: offered and allowed only in edit mode.
EDIT_TOOLS = ("replace_in_file", "edit_file")

# Read-only navigation tools (navigation.py): allowed in every mode.
NAVIGATION_TOOLS = ("find_files", "list_symbols", "find_symbol", "module_dependencies",
                    "git_state")


class ModelError(Exception):
    """Ollama could not be reached or returned an unusable HTTP response."""


class InvalidActionError(Exception):
    """The model answered, but not with a valid action.

    `raw` holds the model's text so the controller can show it back to the
    model and ask it to try again.
    """

    def __init__(self, message, raw=""):
        super().__init__(message)
        self.raw = raw


@dataclass
class Action:
    """One action requested by the model."""

    tool: str
    arguments: dict = field(default_factory=dict)
    summary: str = ""  # only used by "done"
    raw: str = ""      # the model's original reply, for the conversation history


# ----- system prompt ----------------------------------------------------------

TOOL_HELP = {
    "list_files": ('{"path": "."}', 'list files under a folder ("path" is optional)'),
    "read_file": ('{"path": "src/app.py"}', "read one file"),
    "search": ('{"query": "total", "path": "."}',
               'find lines containing this exact text ("path" is optional)'),
    "find_files": ('{"pattern": "services"}',
                   "find files/modules by part of their path, a glob like \"tests/test_*.py\", "
                   "or a module name like \"service_layer.services\""),
    "list_symbols": ('{"path": "src/app.py"}',
                     "list the classes, functions and methods in one Python file, with line "
                     "numbers"),
    "find_symbol": ('{"name": "allocate"}',
                    'find where a class, function or method is defined; "Batch.allocate" '
                    "finds one method"),
    "module_dependencies": ('{"path": "src/app.py"}',
                            "show what a Python module imports and which modules import it"),
    "git_state": ("{}", "show the Git branch, commit, and changed or untracked files"),
    "replace_in_file": ('{"path": "src/app.py", "old": "<exact lines to change>", '
                        '"new": "<replacement lines>"}',
                        'change part of a file: "old" must be copied EXACTLY from the '
                        "file (same spaces) and appear only once. Preferred way to edit"),
    "edit_file": ('{"path": "src/app.py", "content": "<full new file>"}',
                  "replace the WHOLE file. Use it to CREATE a new file. For a file "
                  "that already exists you must read_file it first, and even then "
                  "replace_in_file is the safer choice"),
    # The example has no "name": small models copy examples literally, and a
    # hard-coded name would make them run that check instead of the default.
    "run_check": ("{}",
                  'run the tests ({default}). Leave "arguments" empty unless the task '
                  'asks for another check. Available: {checks}'),
    "done": ("{}", 'finish; put your "summary" next to "arguments"'),
}

COMMON_RULES = [
    "Work only inside this repository. Paths are relative to the repository "
    'root, e.g. "src/app.py". Never use absolute paths or "..".',
    "Never pretend you have read a file or seen output. If you need to know "
    "what a file contains, call read_file. Base every statement on tool results.",
    "Request exactly one action per reply, then wait for its result.",
    "To find code, use find_symbol, find_files and module_dependencies before "
    "reading whole files. A change to a function can affect the modules that import it.",
]

EDIT_RULES = [
    "Read a file before you edit it. Edit existing files with replace_in_file: "
    'copy "old" exactly from the read_file result, including indentation, and '
    'give enough lines that it is unique. Put the complete changed lines in "new".',
    "Never rewrite a whole existing file with edit_file, and never write a "
    'placeholder such as "# existing code" or "...": that deletes real code. '
    "edit_file is refused for a file you have not read in this run.",
    "Make the smallest change that solves the task. Do not reformat, rename "
    "or refactor unrelated code.",
    "After editing, read the file again if you are unsure the change is right.",
    "Before you call done, run a check with run_check and read the result. "
    "If it fails, fix the problem and run the check again.",
    "Do not call done before you have made the change the task asks for.",
    "Be honest in your summary. If the check still fails or you could not "
    "finish, say so.",
]

READONLY_RULES = [
    "This run is READ-ONLY. You cannot change any file. Investigate, run a "
    "check if useful, then call done and report what you found and the "
    "change you would recommend.",
]

RESPONSE_FORMAT = """\
RESPONSE FORMAT
Reply with exactly ONE JSON object and nothing else: no explanation, no markdown.

{"tool": "<tool name>", "arguments": {...}}

Example:
{"tool": "read_file", "arguments": {"path": "orders/pricing.py"}}

When you are finished:
{"tool": "done", "arguments": {}, "summary": "<what you found or changed, and the check result>"}"""


def build_system_prompt(mode="edit", checks=("unittest", "pytest")):
    """Write the system prompt that explains the tools, rules and format.

    `checks` are the run_check names; the first one is the default.
    """
    tool_names = [name for name in TOOLS if mode == "edit" or name not in EDIT_TOOLS]
    tool_lines = []
    for name in tool_names:
        example, description = TOOL_HELP[name]
        description = description.format(checks=", ".join(checks), default=checks[0])
        tool_lines.append(f"- {name}  arguments: {example}  -> {description}")

    rules = COMMON_RULES + (EDIT_RULES if mode == "edit" else READONLY_RULES)
    rule_lines = [f"{number}. {rule}" for number, rule in enumerate(rules, start=1)]

    return "\n".join([
        "You are a coding assistant working inside ONE software repository.",
        "",
        "You cannot see the repository or run anything yourself. You work by",
        "asking the controller to perform one action at a time. The controller",
        "runs it and replies with the result. Then you choose the next action.",
        "",
        "AVAILABLE TOOLS",
        *tool_lines,
        "",
        "RULES",
        *rule_lines,
        "",
        RESPONSE_FORMAT,
    ])


def initial_messages(task, mode="edit", checks=("unittest", "pytest"), context=""):
    """Return the first two messages of a conversation: system prompt + task.

    `context` is optional text (e.g. files the user selected) added after the task.
    """
    request = f"Task: {task}"
    if context:
        request += "\n\n" + context
    return [
        {"role": "system", "content": build_system_prompt(mode, checks)},
        {"role": "user", "content": request},
    ]


# ----- parsing the model's reply ----------------------------------------------

def validate_tool_call(tool, arguments):
    """Check a tool name and its arguments against TOOLS.

    Raises InvalidActionError with a clear message if anything is wrong.
    Used by parse_action and again by the controller before executing.
    """
    if tool not in TOOLS:
        raise InvalidActionError(f"Unknown tool {tool!r}. Valid tools: {', '.join(TOOLS)}")
    if not isinstance(arguments, dict):
        raise InvalidActionError('"arguments" must be a JSON object')

    allowed = TOOLS[tool]
    for name, value in arguments.items():
        if name not in allowed:
            raise InvalidActionError(f"{tool} does not take argument {name!r}")
        if not isinstance(value, str):
            raise InvalidActionError(f"Argument {name!r} must be a string")
    for name, required in allowed.items():
        if required and name not in arguments:
            raise InvalidActionError(f"{tool} needs argument {name!r}")


def parse_action(text):
    """Turn the model's reply into an Action, or raise InvalidActionError.

    We only trust the reply after checking every part of it. Small models
    sometimes wrap JSON in ```json fences or add a sentence around it, so
    if the whole reply is not JSON we try the text from the first "{" to
    the last "}".
    """
    if not text or not text.strip():
        raise InvalidActionError("The reply was empty", raw=text or "")

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end < start:
            raise InvalidActionError("The reply contains no JSON object", raw=text) from None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as error:
            raise InvalidActionError(f"The reply is not valid JSON: {error}", raw=text) from None

    if not isinstance(data, dict):
        raise InvalidActionError("The reply must be a JSON object", raw=text)

    tool = data.get("tool")
    arguments = data.get("arguments", {})
    try:
        validate_tool_call(tool, arguments)
    except InvalidActionError as error:
        raise InvalidActionError(str(error), raw=text) from None

    summary = data.get("summary") or arguments.pop("summary", "")
    if not isinstance(summary, str):
        raise InvalidActionError('"summary" must be a string', raw=text)

    return Action(tool=tool, arguments=arguments, summary=summary, raw=text)


# ----- talking to Ollama ------------------------------------------------------

class ModelClient:
    """Sends a conversation to a local Ollama server and returns an Action."""

    def __init__(self, settings: OllamaSettings):
        """Store how to reach Ollama (URL, model name, timeout)."""
        self.settings = settings

    def request_action(self, messages, cancel=None):
        """Ask the model for its next action.

        Raises ModelError if Ollama cannot be used, InvalidActionError if the
        model's reply is not a valid action, or CancelledError if the run was
        cancelled while waiting.
        """
        return parse_action(self.chat(messages, cancel))

    def chat(self, messages, cancel=None):
        """Send `messages` to Ollama and return the reply text.

        Without `cancel` this blocks until Ollama answers. With `cancel`, the
        request runs in a worker thread and this polls the cancel source while
        it waits, so a cancellation from another process is noticed within
        POLL_SECONDS instead of after a possibly very long generation.
        """
        if cancel is None:
            return self._chat(messages)
        return self._chat_cancellable(messages, cancel)

    def _chat_cancellable(self, messages, cancel):
        """Wait for Ollama in a worker thread, watching for cancellation.

        A local model cannot be told "never mind": there is no way to abort a
        generation that is already running. So on cancellation this abandons
        the request instead of waiting for it. The thread is a daemon, so it
        cannot hold the process open, and -- the part that matters -- its
        result is dropped on the floor. A reply that arrives after the run was
        cancelled can therefore never turn into another action.
        """
        outcome = {}

        def worker():
            try:
                outcome["text"] = self._chat(messages)
            except BaseException as error:        # noqa: BLE001 - re-raised below
                outcome["error"] = error

        thread = threading.Thread(target=worker, daemon=True,
                                  name="ollama-request")
        thread.start()
        try:
            wait_for(lambda: not thread.is_alive(), cancel=cancel,
                     timeout=self.settings.timeout_seconds)
        except CancelledError:
            # The thread is left to finish into the void; nothing reads `outcome`.
            raise
        if "error" in outcome:
            raise outcome["error"]
        if "text" not in outcome:
            raise ModelError(
                f"Ollama did not answer within {self.settings.timeout_seconds} seconds")
        return outcome["text"]

    def _chat(self, messages):
        """Send `messages` to Ollama's /api/chat and return the reply text."""
        url = self.settings.base_url.rstrip("/") + "/api/chat"
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "stream": False,   # one complete answer instead of pieces
            "format": "json",  # ask Ollama to force valid JSON output
            "options": {"temperature": self.settings.temperature,
                        "num_ctx": self.settings.num_ctx},
        }

        try:
            response = requests.post(url, json=payload, timeout=self.settings.timeout_seconds)
        except requests.exceptions.ConnectionError:
            raise ModelError(
                f"Cannot connect to Ollama at {self.settings.base_url}. "
                "Is it running? Start it with: ollama serve"
            ) from None
        except requests.exceptions.Timeout:
            raise ModelError(
                f"Ollama did not answer within {self.settings.timeout_seconds} seconds"
            ) from None
        except requests.exceptions.RequestException as error:
            raise ModelError(f"Request to Ollama failed: {error}") from None

        if response.status_code != 200:
            message = f"Ollama returned HTTP {response.status_code}: {_error_detail(response)}"
            if response.status_code == 404:
                message += f". Is the model installed? Try: ollama pull {self.settings.model}"
            raise ModelError(message)

        try:
            return response.json()["message"]["content"]
        except (ValueError, KeyError, TypeError):
            raise ModelError("Unexpected response from Ollama: no message content") from None


def _error_detail(response):
    """Pull a readable error message out of a failed Ollama response."""
    try:
        return response.json()["error"]
    except (ValueError, KeyError, TypeError):
        return response.text[:200] or "(no details)"


class Blocked:
    """A scripted reply that hangs until the run is cancelled (or released).

    This is how cancellation during a model request is tested without a live
    model: the controller asks for an action, the scripted client waits here,
    the test cancels, and the wait raises CancelledError exactly as a real
    interrupted request would.
    """

    def __init__(self, action=None, timeout=10):
        self.action = action
        self.timeout = timeout
        self.waiting = threading.Event()   # set once the client is inside the wait
        self.released = threading.Event()  # set to let it answer after all

    def release(self):
        self.released.set()


class ScriptedModelClient:
    """Fake model for tests and --offline: returns predefined replies in order.

    Each reply can be:
      - a dict, e.g. {"tool": "read_file", "arguments": {"path": "hello.py"}}
      - a str, i.e. the raw text a real model might send (even broken JSON)
      - an Action, which is returned as-is, skipping parse_action. Tests use
        this to prove the controller does its own checks.

    Dicts and strings go through parse_action, exactly like real replies.
    Nothing here is random and nothing contacts Ollama, so tests are repeatable.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.received = []  # a copy of the conversation from every call

    @property
    def calls(self):
        """How many times request_action was called."""
        return len(self.received)

    def request_action(self, messages, cancel=None):
        """Record the conversation, then return the next predefined reply.

        `cancel` is honoured so cancellation tests need no live model: a reply
        may be a Blocked marker, which waits until the run is cancelled.
        """
        self.received.append(copy.deepcopy(messages))
        if not self.replies:
            raise ModelError("The scripted model has no replies left")

        reply = self.replies.pop(0)
        if isinstance(reply, Blocked):
            # Behave like a model that is thinking: wait, watching for cancel.
            reply.waiting.set()
            wait_for(reply.released.is_set, cancel=cancel, timeout=reply.timeout)
            if reply.action is None:
                raise ModelError("The blocked reply was released with nothing to say")
            reply = reply.action
        if isinstance(reply, Action):
            return reply
        if isinstance(reply, dict):
            reply = json.dumps(reply)
        return parse_action(reply)


if __name__ == "__main__":
    # Quick manual check that Ollama works: python model_client.py llama3.1
    import sys

    model = sys.argv[1] if len(sys.argv) > 1 else "llama3.1"
    client = ModelClient(OllamaSettings(model=model))
    try:
        print(client.request_action(initial_messages("List the files.", mode="readonly")))
    except (ModelError, InvalidActionError) as error:
        print(f"Error: {error}")
        sys.exit(1)
