"""Tests for model_client.py.

No real Ollama server is needed: requests.post is replaced with a fake
that records what was sent and returns a prepared answer.
"""

import json

import pytest
import requests

from config import OllamaSettings
from model_client import (
    Action,
    InvalidActionError,
    ModelClient,
    ModelError,
    ScriptedModelClient,
    build_system_prompt,
    initial_messages,
    parse_action,
)


# ----- parse_action: valid replies --------------------------------------------

def test_parse_read_file():
    action = parse_action('{"tool": "read_file", "arguments": {"path": "orders/pricing.py"}}')
    assert action.tool == "read_file"
    assert action.arguments == {"path": "orders/pricing.py"}


def test_parse_done_with_summary():
    action = parse_action('{"tool": "done", "arguments": {}, "summary": "Fixed it."}')
    assert action.tool == "done"
    assert action.summary == "Fixed it."


def test_parse_missing_arguments_means_empty():
    assert parse_action('{"tool": "list_files"}').arguments == {}


def test_parse_json_inside_code_fence_and_text():
    reply = 'Sure!\n```json\n{"tool": "search", "arguments": {"query": "total"}}\n```'
    assert parse_action(reply).arguments == {"query": "total"}


# ----- parse_action: malformed replies are rejected safely -------------------

@pytest.mark.parametrize("reply, message", [
    ("", "empty"),
    ("I will read the file now.", "no JSON object"),
    ('{"tool": "read_file", "arguments": {"path": "a.py"', "no JSON object"),
    ('{"tool": "read_file", "arguments": {path: "a.py"}}', "not valid JSON"),
    ('[{"tool": "read_file"}]', "must be a JSON object"),
    ('{"tool": "delete_everything", "arguments": {}}', "Unknown tool"),
    ('{"arguments": {"path": "a.py"}}', "Unknown tool"),
    ('{"tool": "read_file", "arguments": "a.py"}', "must be a JSON object"),
    ('{"tool": "read_file", "arguments": {}}', "needs argument 'path'"),
    ('{"tool": "read_file", "arguments": {"path": 42}}', "must be a string"),
    ('{"tool": "read_file", "arguments": {"path": "a", "cmd": "ls"}}', "does not take"),
])
def test_malformed_replies_raise_invalid_action(reply, message):
    with pytest.raises(InvalidActionError, match=message) as error:
        parse_action(reply)
    assert error.value.raw == reply


# ----- system prompt ------------------------------------------------------------

def test_system_prompt_mentions_key_rules():
    prompt = build_system_prompt(mode="edit", checks=("unittest",))
    for text in ["ONE software repository", "read_file", "replace_in_file", "edit_file",
                 "smallest change", "run_check", "ONE JSON object", "unittest"]:
        assert text in prompt


def test_readonly_prompt_hides_edit_file():
    prompt = build_system_prompt(mode="readonly")
    assert "edit_file" not in prompt
    assert "replace_in_file" not in prompt
    assert "READ-ONLY" in prompt


def test_parse_replace_in_file():
    action = parse_action('{"tool": "replace_in_file", "arguments": '
                          '{"path": "a.py", "old": "x = 1", "new": "x = 2"}}')
    assert action.arguments == {"path": "a.py", "old": "x = 1", "new": "x = 2"}
    with pytest.raises(InvalidActionError, match="needs argument 'new'"):
        parse_action('{"tool": "replace_in_file", "arguments": {"path": "a.py", "old": "x"}}')


def test_initial_messages():
    messages = initial_messages("Fix the incorrect total")
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == "Task: Fix the incorrect total"


# ----- ModelClient with a fake Ollama -----------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


def ollama_reply(content):
    return FakeResponse(body={"message": {"role": "assistant", "content": content}})


@pytest.fixture
def client():
    return ModelClient(OllamaSettings(model="llama3.1", base_url="http://ollama:11434/"))


def test_client_sends_request_and_returns_action(client, monkeypatch):
    sent = {}

    def fake_post(url, json, timeout):
        sent.update(url=url, payload=json, timeout=timeout)
        return ollama_reply('{"tool": "list_files", "arguments": {"path": "."}}')

    monkeypatch.setattr(requests, "post", fake_post)
    messages = initial_messages("Fix the incorrect total")

    action = client.request_action(messages)

    assert action == Action(tool="list_files", arguments={"path": "."}, raw=action.raw)
    assert sent["url"] == "http://ollama:11434/api/chat"
    assert sent["payload"]["model"] == "llama3.1"
    assert sent["payload"]["format"] == "json"
    assert sent["payload"]["stream"] is False
    assert sent["payload"]["messages"] == messages
    assert sent["payload"]["options"]["num_ctx"] == 16384
    assert sent["timeout"] == 300


def test_client_rejects_malformed_model_output(client, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: ollama_reply("not json at all"))
    with pytest.raises(InvalidActionError):
        client.request_action([])


def test_ollama_not_running(client, monkeypatch):
    def fake_post(*args, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(ModelError, match="Cannot connect to Ollama.*ollama serve"):
        client.request_action([])


def test_ollama_timeout(client, monkeypatch):
    def fake_post(*args, **kwargs):
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(ModelError, match="did not answer within 300 seconds"):
        client.request_action([])


def test_model_not_found(client, monkeypatch):
    response = FakeResponse(404, body={"error": "model 'llama3.1' not found"})
    monkeypatch.setattr(requests, "post", lambda *a, **k: response)
    with pytest.raises(ModelError, match="HTTP 404.*ollama pull llama3.1"):
        client.request_action([])


def test_unexpected_ollama_body(client, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse(body={"oops": 1}))
    with pytest.raises(ModelError, match="no message content"):
        client.request_action([])


# ----- ScriptedModelClient (offline mode) --------------------------------------

def test_scripted_client_returns_replies_in_order():
    scripted = ScriptedModelClient([
        json.dumps({"tool": "read_file", "arguments": {"path": "a.py"}}),
        json.dumps({"tool": "done", "arguments": {}, "summary": "ok"}),
    ])
    assert scripted.request_action([]).tool == "read_file"
    assert scripted.request_action([]).tool == "done"
    with pytest.raises(ModelError, match="no replies left"):
        scripted.request_action([])


def test_scripted_client_accepts_dicts_strings_and_actions():
    direct = Action(tool="anything", arguments={})
    scripted = ScriptedModelClient([
        {"tool": "read_file", "arguments": {"path": "hello.py"}},
        {"tool": "run_check", "arguments": {}},
        '{"tool": "done", "arguments": {}, "summary": "Finished"}',
        "broken {json",
        direct,
    ])

    assert scripted.request_action(["m1"]).arguments == {"path": "hello.py"}
    assert scripted.request_action(["m2"]).tool == "run_check"
    assert scripted.request_action(["m3"]).summary == "Finished"
    with pytest.raises(InvalidActionError):
        scripted.request_action(["m4"])
    assert scripted.request_action(["m5"]) is direct   # passed through unchecked
    assert scripted.calls == 5
    assert scripted.received[0] == ["m1"]
