"""Tests for the official-openai-SDK LLM transport.

The transport must speak the official SDK's client surface
(``chat.completions.create`` / ``models.list``) and never re-implement
HTTP or SSE parsing itself.  The SDK is imported lazily: importing the
module, constructing the transport and composing an offline runtime must
all succeed without ``openai`` installed, and every test below drives the
transport through an injected fake client factory so no network is ever
touched.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from prism.llm import (
    LLMTransportError,
    OpenAISDKTransport,
    Provider,
    RetryableLLMError,
    TransportResponse,
)


def provider(base_url="https://llm.example.test/v1"):
    return Provider(
        name="example",
        base_url=base_url,
        api_key_env="EXAMPLE_API_KEY",
        default_model="default-model",
    )


VOLCANO_V3_BASE_URL = "https://ark.cn.volces.com/api/coding/v3"


class FakeStatusError(Exception):
    """Duck-typed stand-in for ``openai.APIStatusError``."""

    def __init__(self, status_code, message="provider exploded"):
        super().__init__(message)
        self.status_code = status_code


class FakeCompletions:
    def __init__(self, *, response=None, stream_chunks=None, error=None):
        self.calls = []
        self._response = response
        self._stream_chunks = stream_chunks
        self._error = error

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if self._stream_chunks is not None:

            async def chunk_iterator():
                for chunk in self._stream_chunks:
                    yield chunk

            return chunk_iterator()
        return self._response


class FakeModels:
    def __init__(self, *, error=None):
        self.calls = []
        self._error = error

    async def list(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return SimpleNamespace(data=[])


class FakeClient:
    def __init__(self, completions=None, models=None):
        self.chat = SimpleNamespace(completions=completions or FakeCompletions())
        self.models = models or FakeModels()


def completion(content="answer", finish_reason="stop"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
    )


def chunk(content=None, finish_reason=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=content),
                finish_reason=finish_reason,
            )
        ]
    )


def make_transport(client, *, recordings=None, **kwargs):
    def client_factory(*, api_key, base_url, timeout):
        if recordings is not None:
            recordings.append(
                {"api_key": api_key, "base_url": base_url, "timeout": timeout}
            )
        return client

    return OpenAISDKTransport(client_factory=client_factory, **kwargs)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------- lazy


def test_importing_transport_module_does_not_import_openai():
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        "import sys;"
        "import prism.llm, prism.llm.transport, prism.runtime;"
        "print('imported' if sys.modules.get('openai') is not None"
        " else 'absent')"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = repo_src
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=environment,
        check=True,
    )
    assert "absent" in result.stdout


def test_constructing_transport_and_completing_fails_clearly_without_sdk(
    monkeypatch,
):
    monkeypatch.setitem(sys.modules, "openai", None)
    transport = OpenAISDKTransport()

    with pytest.raises(LLMTransportError, match="openai-sdk"):
        run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "m", "prompt": "p"},
                timeout=1,
            )
        )


def test_injected_client_factory_keeps_the_transport_usable_without_sdk(
    monkeypatch,
):
    monkeypatch.setitem(sys.modules, "openai", None)
    client = FakeClient(completions=FakeCompletions(response=completion("ok")))
    transport = make_transport(client)

    response = run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "m", "prompt": "p"},
            timeout=1,
        )
    )
    assert response == TransportResponse(text="ok")


# ---------------------------------------------------------------- base_url


def test_base_url_is_passed_to_the_sdk_client_verbatim():
    recordings = []
    client = FakeClient(completions=FakeCompletions(response=completion()))
    transport = make_transport(client, recordings=recordings)

    run(
        transport.complete(
            provider=provider(VOLCANO_V3_BASE_URL),
            api_key="secret",
            payload={"model": "m", "prompt": "p"},
            timeout=9.5,
        )
    )

    assert len(recordings) == 1
    # The Volcano coding endpoint already carries its /api/coding/v3 version
    # segment: no /v1 may be appended or rewritten by the transport.
    assert recordings[0]["base_url"] == VOLCANO_V3_BASE_URL
    assert recordings[0]["api_key"] == "secret"
    assert recordings[0]["timeout"] == 9.5


@pytest.mark.parametrize(
    "base_url",
    [
        "https://llm.example.test",
        "https://llm.example.test/",
        "https://llm.example.test/v1",
        "https://ark.cn.volces.com/api/coding/v3",
    ],
)
def test_every_base_url_shape_reaches_the_factory_unchanged(base_url):
    recordings = []
    client = FakeClient(completions=FakeCompletions(response=completion()))
    transport = make_transport(client, recordings=recordings)

    run(
        transport.complete(
            provider=provider(base_url),
            api_key="secret",
            payload={"model": "m", "prompt": "p"},
            timeout=1,
        )
    )
    assert recordings[0]["base_url"] == base_url


def test_client_is_reused_per_provider_and_rebuilt_when_the_key_changes():
    recordings = []
    client = FakeClient(completions=FakeCompletions(response=completion()))
    transport = make_transport(client, recordings=recordings)
    selected = provider()

    for api_key in ("first-secret", "first-secret"):
        run(
            transport.complete(
                provider=selected,
                api_key=api_key,
                payload={"model": "m", "prompt": "p"},
                timeout=1,
            )
        )
    run(
        transport.complete(
            provider=selected,
            api_key="second-secret",
            payload={"model": "m", "prompt": "p"},
            timeout=1,
        )
    )
    assert [item["api_key"] for item in recordings] == [
        "first-secret",
        "second-secret",
    ]


# --------------------------------------------------------- non-stream path


def test_complete_uses_official_non_streaming_chat_completions():
    client = FakeClient(completions=FakeCompletions(response=completion("answer")))
    transport = make_transport(client)

    response = run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "chosen-model", "prompt": "Say hello"},
            timeout=7.5,
        )
    )

    assert response == TransportResponse(text="answer")
    (call,) = client.chat.completions.calls
    assert call["model"] == "chosen-model"
    assert call["messages"] == [{"role": "user", "content": "Say hello"}]
    assert call["stream"] is False
    assert call["timeout"] == 7.5
    # The key travels only through the client, never through the request.
    assert "secret" not in str(call)


def test_json_mode_uses_the_sdk_response_format_without_changing_stream_mode():
    client = FakeClient(completions=FakeCompletions(response=completion("{}")))
    transport = make_transport(client, json_mode=True)

    response = run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "chosen-model", "prompt": "Return JSON"},
            timeout=7.5,
        )
    )

    assert response == TransportResponse(text="{}")
    (call,) = client.chat.completions.calls
    assert call["stream"] is False
    assert call["response_format"] == {"type": "json_object"}


def test_complete_passes_messages_through_when_payload_provides_them():
    client = FakeClient(completions=FakeCompletions(response=completion()))
    transport = make_transport(client)
    messages = [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello"},
    ]

    run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "m", "messages": messages},
            timeout=1,
        )
    )
    (call,) = client.chat.completions.calls
    assert call["messages"] == messages


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-mapping",
        {"prompt": "p"},
        {"model": "  ", "prompt": "p"},
        {"model": "m"},
        {"model": "m", "prompt": 5},
        {"model": "m", "messages": "chat"},
    ],
)
def test_invalid_payloads_fail_before_any_client_call(payload):
    client = FakeClient(completions=FakeCompletions(response=completion()))
    transport = make_transport(client)

    with pytest.raises(LLMTransportError):
        run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload=payload,
                timeout=1,
            )
        )
    assert client.chat.completions.calls == []


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(choices=[]),
        SimpleNamespace(choices=[SimpleNamespace(message=None)]),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=7))]
        ),
        SimpleNamespace(choices=None),
    ],
)
def test_malformed_non_stream_response_schema_is_a_clear_error(response):
    client = FakeClient(completions=FakeCompletions(response=response))
    transport = make_transport(client)

    with pytest.raises(LLMTransportError, match="response"):
        run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "m", "prompt": "p"},
                timeout=1,
            )
        )


# ------------------------------------------------------------- stream path


def streaming_transport(chunks, **kwargs):
    client = FakeClient(completions=FakeCompletions(stream_chunks=chunks))
    return make_transport(client, **kwargs), client


def test_stream_mode_collects_delta_content_and_requires_finish_reason():
    transport, client = streaming_transport(
        [
            chunk(content="Hel"),
            chunk(content="lo "),
            chunk(content=None, finish_reason=None),
            chunk(content="world", finish_reason=None),
            chunk(content=None, finish_reason="stop"),
        ],
        stream=True,
    )

    response = run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "m", "prompt": "p"},
            timeout=4.5,
        )
    )
    assert response == TransportResponse(text="Hello world")
    (call,) = client.chat.completions.calls
    assert call["stream"] is True
    assert call["timeout"] == 4.5


def test_stream_json_mode_uses_the_sdk_response_format():
    transport, client = streaming_transport(
        [chunk(content="{}"), chunk(finish_reason="stop")],
        stream=True,
        json_mode=True,
    )

    response = run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "m", "prompt": "p"},
            timeout=1,
        )
    )

    assert response.text == "{}"
    (call,) = client.chat.completions.calls
    assert call["response_format"] == {"type": "json_object"}


def test_stream_mode_accepts_length_finish_reason():
    transport, _ = streaming_transport(
        [chunk(content="partial"), chunk(finish_reason="length")],
        stream=True,
    )
    response = run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "m", "prompt": "p"},
            timeout=1,
        )
    )
    assert response.text == "partial"


def test_stream_without_explicit_finish_reason_is_rejected():
    transport, _ = streaming_transport(
        [chunk(content="loose end")],
        stream=True,
    )
    with pytest.raises(LLMTransportError, match="finish_reason"):
        run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "m", "prompt": "p"},
                timeout=1,
            )
        )


def test_stream_chunks_without_choices_are_ignored():
    transport, _ = streaming_transport(
        [
            SimpleNamespace(choices=[]),
            chunk(content="kept"),
            chunk(finish_reason="stop"),
        ],
        stream=True,
    )
    response = run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "m", "prompt": "p"},
            timeout=1,
        )
    )
    assert response.text == "kept"


def test_default_transport_is_not_streaming():
    client = FakeClient(completions=FakeCompletions(response=completion("plain")))
    transport = make_transport(client)
    run(
        transport.complete(
            provider=provider(),
            api_key="secret",
            payload={"model": "m", "prompt": "p"},
            timeout=1,
        )
    )
    (call,) = client.chat.completions.calls
    assert call["stream"] is False


# ------------------------------------------------------------ error mapping


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 502, 599])
def test_retryable_http_statuses_raise_retryable_error(status):
    client = FakeClient(
        completions=FakeCompletions(error=FakeStatusError(status))
    )
    transport = make_transport(client)

    with pytest.raises(RetryableLLMError, match=str(status)):
        run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "m", "prompt": "p"},
                timeout=1,
            )
        )


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_other_http_statuses_are_clear_and_non_retryable(status):
    client = FakeClient(
        completions=FakeCompletions(error=FakeStatusError(status))
    )
    transport = make_transport(client)

    with pytest.raises(LLMTransportError, match=str(status)) as caught:
        run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "m", "prompt": "p"},
                timeout=1,
            )
        )
    assert not isinstance(caught.value, RetryableLLMError)


def test_timeout_errors_are_retryable_without_the_openai_package(monkeypatch):
    class APITimeoutError(Exception):
        pass

    monkeypatch.setitem(sys.modules, "openai", None)
    client = FakeClient(
        completions=FakeCompletions(error=APITimeoutError("timed out"))
    )
    transport = make_transport(client)

    with pytest.raises(RetryableLLMError, match="timed out"):
        run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "m", "prompt": "p"},
                timeout=1,
            )
        )


def test_error_messages_never_leak_key_prompt_or_response_body():
    api_key = "sk-full-secret-value"
    prompt = "PROMPT-MARKER-do-not-leak"
    body = "BODY-MARKER-do-not-leak"
    error = FakeStatusError(
        401,
        f"request failed for {api_key} with prompt {prompt} and body {body}",
    )
    client = FakeClient(completions=FakeCompletions(error=error))
    transport = make_transport(client)

    with pytest.raises(LLMTransportError) as caught:
        run(
            transport.complete(
                provider=provider(),
                api_key=api_key,
                payload={"model": "m", "prompt": prompt},
                timeout=1,
            )
        )
    message = str(caught.value)
    assert api_key not in message
    assert prompt not in message
    assert body not in message


def test_unexpected_errors_become_non_retryable_named_transport_errors():
    client = FakeClient(completions=FakeCompletions(error=RuntimeError("boom")))
    transport = make_transport(client)

    with pytest.raises(LLMTransportError, match="RuntimeError") as caught:
        run(
            transport.complete(
                provider=provider(),
                api_key="secret",
                payload={"model": "m", "prompt": "p"},
                timeout=1,
            )
        )
    assert not isinstance(caught.value, RetryableLLMError)


# ----------------------------------------------------------- test_connection


def test_connection_uses_the_sdk_models_list_probe():
    client = FakeClient()
    transport = make_transport(client)

    connected = run(
        transport.test_connection(
            provider=provider(VOLCANO_V3_BASE_URL),
            api_key="connection-secret",
            timeout=2.5,
        )
    )
    assert connected is True
    (call,) = client.models.calls
    assert call["timeout"] == 2.5
    assert client.chat.completions.calls == []


def test_connection_maps_errors_like_complete():
    client = FakeClient(models=FakeModels(error=FakeStatusError(429)))
    transport = make_transport(client)

    with pytest.raises(RetryableLLMError):
        run(
            transport.test_connection(
                provider=provider(),
                api_key="secret",
                timeout=1,
            )
        )


def test_connection_without_sdk_fails_clearly(monkeypatch):
    monkeypatch.setitem(sys.modules, "openai", None)
    transport = OpenAISDKTransport()

    with pytest.raises(LLMTransportError, match="openai-sdk"):
        run(
            transport.test_connection(
                provider=provider(),
                api_key="secret",
                timeout=1,
            )
        )
