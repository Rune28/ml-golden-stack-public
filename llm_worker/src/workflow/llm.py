"""The one interface the platform calls language models through.

chat() and embed() over the OpenAI-compatible API, which Ollama, llama.cpp
and vLLM all serve. The server and the models come from the environment
(LLM_BASE_URL, LLM_CHAT_MODEL, LLM_EMBED_MODEL): a server in the cluster or a
managed one is another base URL, not another client, and a model is a name
that can also be given per call.

Blocking, standard library only: it runs in activities on the worker's
thread pool, never in workflow code.
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


class LLMError(Exception):
    """The server answered with an error or could not be reached."""


@dataclass(frozen=True)
class Reply:
    text: str
    model: str
    # Models that think before they answer (qwen3) return the thinking in a
    # field of its own; it is kept apart so it never leaks into the answer.
    reasoning: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class LLM:
    base_url: str
    chat_model: str
    embed_model: str
    timeout_seconds: float = 300.0

    @classmethod
    def from_env(cls) -> "LLM":
        return cls(
            base_url=os.environ["LLM_BASE_URL"],
            chat_model=os.environ["LLM_CHAT_MODEL"],
            embed_model=os.environ["LLM_EMBED_MODEL"],
        )

    def chat(self, messages: list[dict], model: str | None = None) -> Reply:
        """messages: [{"role": "system" | "user" | "assistant", "content": str}, ...]"""
        body = self._post("/chat/completions", {"model": model or self.chat_model, "messages": messages})
        message = body["choices"][0]["message"]
        usage = body.get("usage") or {}
        return Reply(
            text=message.get("content") or "",
            model=body.get("model") or model or self.chat_model,
            reasoning=message.get("reasoning") or "",
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """One vector per text, in the order of the texts."""
        body = self._post("/embeddings", {"model": model or self.embed_model, "input": texts})
        data = sorted(body["data"], key=lambda d: d.get("index", 0))
        if len(data) != len(texts):
            raise LLMError(f"/embeddings: {len(data)} vectors for {len(texts)} texts")
        return [d["embedding"] for d in data]

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base_url.rstrip("/") + path,
            data=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode(errors="replace")
            raise LLMError(f"{path}: HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"{path}: {self.base_url} unreachable: {e.reason}") from e


def check() -> None:
    """`make llm-check`: one answer and one embedding, from inside the cluster."""
    llm = LLM.from_env()
    reply = llm.chat([{"role": "user", "content": "Reply with one word: the capital of France?"}])
    vector = llm.embed(["test"])[0]
    print(json.dumps({
        "base_url": llm.base_url,
        "chat": {"model": reply.model, "answer": reply.text, "tokens": reply.completion_tokens},
        "embed": {"model": llm.embed_model, "dimensions": len(vector)},
    }, ensure_ascii=False))


if __name__ == "__main__":
    check()
