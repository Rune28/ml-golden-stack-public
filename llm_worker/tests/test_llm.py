import io
import json
import urllib.error

import pytest

from workflow import llm as llm_module
from workflow.llm import LLM, LLMError

LLM_ = LLM(base_url="http://llm.llm.svc/v1/", chat_model="chat-default", embed_model="embed-default")


class FakeServer:
    """Stands in for urlopen: records requests, answers with canned JSON or an error."""

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.requests = answer, error, []

    def __call__(self, request, timeout):
        self.requests.append((request.full_url, json.loads(request.data)))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.answer).encode())


@pytest.fixture
def server(monkeypatch):
    def install(**kwargs):
        fake = FakeServer(**kwargs)
        monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake)
        return fake

    return install


def test_chat_keeps_reasoning_apart_from_the_answer(server):
    fake = server(answer={
        "model": "chat-default",
        "choices": [{"message": {"role": "assistant", "content": "Paris", "reasoning": "The user asks..."}}],
        "usage": {"prompt_tokens": 19, "completion_tokens": 118},
    })

    reply = LLM_.chat([{"role": "user", "content": "capital of France?"}])

    assert (reply.text, reply.reasoning, reply.completion_tokens) == ("Paris", "The user asks...", 118)
    url, body = fake.requests[0]
    assert url == "http://llm.llm.svc/v1/chat/completions"
    assert body["model"] == "chat-default"


def test_a_model_can_be_chosen_per_call(server):
    fake = server(answer={"choices": [{"message": {"content": "ok"}}]})

    reply = LLM_.chat([{"role": "user", "content": "hi"}], model="other-model")

    assert fake.requests[0][1]["model"] == "other-model" and reply.model == "other-model"


def test_embed_returns_vectors_in_the_order_of_the_texts(server):
    server(answer={"data": [{"index": 1, "embedding": [0.2]}, {"index": 0, "embedding": [0.1]}]})

    assert LLM_.embed(["a", "b"]) == [[0.1], [0.2]]


def test_embed_rejects_a_missing_vector(server):
    server(answer={"data": [{"index": 0, "embedding": [0.1]}]})

    with pytest.raises(LLMError, match="1 vectors for 2 texts"):
        LLM_.embed(["a", "b"])


def test_server_errors_carry_the_status_and_the_reason(server):
    server(error=urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO(b'{"error":"model not found"}')))
    with pytest.raises(LLMError, match="HTTP 404.*model not found"):
        LLM_.chat([{"role": "user", "content": "hi"}])

    server(error=urllib.error.URLError("Connection refused"))
    with pytest.raises(LLMError, match="unreachable: Connection refused"):
        LLM_.embed(["a"])
