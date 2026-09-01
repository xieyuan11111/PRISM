import asyncio
import json
import threading
from urllib.error import HTTPError

import pytest
import prism.llm.transport as transport_module

from prism.llm import (
    LLMTransportError,
    OpenAICompatibleTransport,
    Provider,
    RetryableLLMError,
    TransportResponse,
)


class FakeResponse:
    def __init__(self, body=b"", *, status=200):
        self._body = body
        self.status = status
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def provider(base_url="https://llm.example.test"):
    return Provider(
        name="example",
        base_url=base_url,
        api_key_env="EXAMPLE_API_KEY",
        default_model="default-model",
    )


def completion_body(content="answer", *, usage=None):
    document = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        document["usage"] = usage
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    ("base_url", "expected_url"),
    [
        (
            "https://llm.example.test",
            "https://llm.example.test/v1/chat/completions",
        ),
        (
            "https://llm.example.test/",
            "https://llm.example.test/v1/chat/completions",
        ),
        (
            "https://llm.example.test/v1",
            "https://llm.example.test/v1/chat/completions",
        ),
        (
            "https://llm.example.test/v1/",
            "https://llm.example.test/v1/chat/completions",
        ),
    ],
)
def test_complete_posts_openai_request_without_secret_in_body(base_url, expected_url):
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return FakeResponse(completion_body())

    transport = OpenAICompatibleTransport(opener=opener)

    response = asyncio.run(
        transport.complete(
            provider=provider(base_url),
            api_key="super-secret-key",
            payload={"model": "chosen-model", "prompt": "Say hello"},
            timeout=7.5,
        )
    )

    request, timeout = calls[0]
    body = json.loads(request.data)
    assert response == TransportResponse(text="answer")
    assert request.full_url == expected_url
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer super-secret-key"
    assert request.get_header("Content-type") == "application/json"
    assert body == {
        "model": "chosen-model",
        "messages": [{"role": "user", "content": "Say hello"}],
    }
    assert "super-secret-key" not in request.data.decode()
    assert timeout == 7.5


def test_complete_parses_content_when_optional_usage_is_present():
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    transport = OpenAICompatibleTransport(
        opener=lambda request, *, timeout: FakeResponse(
            completion_body("with usage", usage=usage)
        )
    )

    response = asyncio.run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "model", "prompt": "prompt"},
            timeout=1,
        )
    )

    assert isinstance(response, TransportResponse)
    assert response.text == "with usage"


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 599])
def test_retryable_http_statuses_raise_retryable_error(status):
    def opener(request, *, timeout):
        raise HTTPError(request.full_url, status, "failure", {}, None)

    transport = OpenAICompatibleTransport(opener=opener)

    with pytest.raises(RetryableLLMError, match=str(status)):
        asyncio.run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "model", "prompt": "prompt"},
                timeout=1,
            )
        )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_client_errors_are_clear_and_non_retryable(status):
    def opener(request, *, timeout):
        raise HTTPError(request.full_url, status, "failure", {}, None)

    transport = OpenAICompatibleTransport(opener=opener)

    with pytest.raises(LLMTransportError, match=str(status)) as caught:
        asyncio.run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "model", "prompt": "prompt"},
                timeout=1,
            )
        )

    assert not isinstance(caught.value, RetryableLLMError)


def test_timeout_is_retryable_and_exception_does_not_reveal_api_key():
    api_key = "sk-full-secret-value"

    def opener(request, *, timeout):
        raise TimeoutError(f"timed out while using {api_key}")

    transport = OpenAICompatibleTransport(opener=opener)

    with pytest.raises(RetryableLLMError, match="timed out") as caught:
        asyncio.run(
            transport.complete(
                provider=provider(),
                api_key=api_key,
                payload={"model": "model", "prompt": "prompt"},
                timeout=1,
            )
        )

    assert api_key not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"{}",
        b'{"choices": []}',
        b'{"choices": [{"message": {}}]}',
        b'{"choices": [{"message": {"content": 7}}]}',
    ],
)
def test_malformed_json_or_response_schema_is_a_clear_error(body):
    transport = OpenAICompatibleTransport(
        opener=lambda request, *, timeout: FakeResponse(body)
    )

    with pytest.raises(LLMTransportError, match="response"):
        asyncio.run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "model", "prompt": "prompt"},
                timeout=1,
            )
        )


def test_complete_runs_blocking_opener_outside_event_loop_thread():
    event_loop_thread = threading.get_ident()
    opener_threads = []

    def opener(request, *, timeout):
        opener_threads.append(threading.get_ident())
        return FakeResponse(completion_body())

    transport = OpenAICompatibleTransport(opener=opener)
    asyncio.run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "model", "prompt": "prompt"},
            timeout=1,
        )
    )

    assert opener_threads
    assert opener_threads[0] != event_loop_thread


def test_complete_parses_response_outside_event_loop_thread(monkeypatch):
    event_loop_thread = threading.get_ident()
    parser_threads = []
    real_loads = json.loads

    def recording_loads(value):
        parser_threads.append(threading.get_ident())
        return real_loads(value)

    monkeypatch.setattr(transport_module.json, "loads", recording_loads)
    transport = OpenAICompatibleTransport(
        opener=lambda request, *, timeout: FakeResponse(completion_body())
    )

    asyncio.run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "model", "prompt": "prompt"},
            timeout=1,
        )
    )

    assert parser_threads
    assert parser_threads[0] != event_loop_thread


def test_connection_uses_injected_opener_and_minimal_models_request():
    calls = []
    response = FakeResponse(status=200)

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return response

    transport = OpenAICompatibleTransport(opener=opener)

    connected = asyncio.run(
        transport.test_connection(
            provider=provider("https://llm.example.test/v1/"),
            api_key="connection-secret",
            timeout=2.5,
        )
    )

    request, timeout = calls[0]
    assert connected is True
    assert request.full_url == "https://llm.example.test/v1/models"
    assert request.get_method() == "GET"
    assert request.data is None
    assert request.get_header("Authorization") == "Bearer connection-secret"
    assert timeout == 2.5
    assert response.closed is True
