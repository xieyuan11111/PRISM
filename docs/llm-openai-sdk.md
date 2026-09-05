# PRISM LLM transport: the official `openai` SDK

Status: implemented (baseline `a0d30ca` follow-up). This note records how
PRISM's own LLM network layer is built exclusively on the official
[`openai` Python SDK](https://pypi.org/project/openai/) and where the
third-party boundaries are.

## One transport, one protocol

PRISM owns exactly one LLM network path:

```
ExtractionService / DebateService / ReportService / AdjudicationService
        └── LLMRouter.complete(role, prompt)          (prism/llm/router.py)
                └── OpenAISDKTransport                (prism/llm/transport.py)
                        └── official AsyncOpenAI client
```

All four task services are constructed with the same router instance by
the composition root (`prism/runtime/composition.py`), and the router
talks to one transport. No service builds its own HTTP client, and no
PRISM code re-implements OpenAI-compatible HTTP or SSE parsing: request
encoding, headers and server-sent-event decoding all live inside the
official SDK.

The previous stdlib-`urllib` transport (`OpenAICompatibleTransport`) was
removed with this change. It was the last hand-rolled protocol code in
PRISM's LLM layer; nothing imports it anymore.

## Opt-in dependency, lazy import

* `pyproject.toml` core `dependencies` stay empty. The SDK is the
  explicit extra `[openai-sdk]` (`openai>=2.24,<3`):
  `pip install "news-prism[openai-sdk]"`.
* Importing `prism.llm`, constructing `OpenAISDKTransport` and composing
  a runtime (including one with configured LLM providers) never import
  `openai` and never touch the network. The import happens inside the
  default client factory, which runs only when a completion or connection
  check is actually attempted.
* Without the package installed, a real transport call fails closed with
  `LLMTransportError` naming the extra to install. Injected fake
  transports (and the `client_factory` seam below) keep everything usable
  offline.

## Transport behaviour

`OpenAISDKTransport(client_factory=None, stream=False)` implements the
`LLMTransport` protocol:

* **`client_factory`** — injectable for tests; called as
  `client_factory(api_key=..., base_url=..., timeout=...)` and must
  return an `AsyncOpenAI`-shaped client. The default factory builds
  `AsyncOpenAI(..., max_retries=0)` — retries stay owned by the router's
  bounded `RetryPolicy`, never by the SDK.
* **`base_url` passthrough** — the configured base URL is forwarded
  verbatim. Providers that already carry their version segment (for
  example Volcano's `.../api/coding/v3` endpoint) must not have `/v1`
  appended; the deleted urllib transport used to do that rewriting.
* **`complete(stream=False)`** — the SDK's non-streaming
  `chat.completions.create`; the response must carry
  `choices[0].message.content` as text or the attempt fails with
  `LLMTransportError`.
* **`complete(stream=True)`** — the SDK's streaming
  `chat.completions.create`; PRISM only consumes
  `choices[0].delta.content` fragments from the SDK-parsed stream and
  requires the stream to end with an explicit `finish_reason` (such as
  `stop`/`length`). A stream that ends without one is a transport error.
* **`test_connection`** — the SDK's body-free `models.list` probe.
* **Error mapping** — HTTP 408/409/425/429 and all 5xx (via the SDK's
  `status_code`) plus timeouts/connection failures map to
  `RetryableLLMError`; everything else is `LLMTransportError`. Raised
  messages contain only the provider name, the status code and the
  exception class name — the SDK's raw message text (which can echo the
  API key, prompt or response body) is never propagated.

## Prompt-profile experiment tool

`tools/run_prompt_profile_experiment.py` evaluates extraction prompt
profiles through this same path: it injects `OpenAISDKTransport(stream=--sdk-stream)`
into `create_runtime`'s `llm_transport` seam, so the run exercises the
real router + SDK transport (no second protocol). The tool is
extraction-only (only the `extract` task role is routed), runs on
`OfflineGraphBackend` (Graphiti never started or imported), and requires
`--execute` for real provider calls; without it, it performs an offline
validation pass and writes a sanitized `run-plan.json`. Its public
artifact is the strictly sanitized `prompt-run-summary.json` produced by
the live acceptance runner's bridge (`build_prompt_run_summary` +
`guard_public_summary` in `tools/run_live_case_acceptance.py`), directly
consumable by `tools/prism_prompt_benchmark.py`.

## Graphiti is a third-party boundary

Graphiti's own LLM calls are NOT PRISM's transport. PRISM configures an
SDK-compatible client for Graphiti only through Graphiti's public
injection seams — `graphiti_client_factory` at
`create_runtime(...)`/`build_graphiti_client(llm_client=...)` in
`src/prism/graph/graphiti_client.py` — and never patches, wraps or
re-implements Graphiti's internal client calls (see
`tools/run_live_case_acceptance.py: build_real_provider_graphiti_factory`
for the live example that injects `OpenAIGenericClient`). When PRISM
migrates its own transport to the official SDK, Graphiti keeps working
unchanged through this seam; upgrades of graphiti-core remain a
third-party decision pinned by the optional `[graphiti]` extra.
