# Contributing to PRISM

## Project boundary

PRISM is a local, auditable policy and academic-discourse evolution tracker. It is not a final-decision engine. Contributions must preserve the distinction between mechanism success, semantic quality and evidence gaps.

## Development environment

```console
python -m pip install -e ".[dev]"
python -m pytest -q
```

Optional extras are explicit:

```text
pdf         Markdown-to-PDF export
webui       NiceGUI and Plotly local UI
openai-sdk  Official OpenAI Python SDK transport
graphiti    Optional Graphiti / Neo4j integration
```

The default core install is offline. Do not make optional services mandatory for core imports or offline tests.

## Native Neo4j route

PRISM does not use Docker or Docker Compose. Optional Graphiti work uses a PRISM-owned native Neo4j launcher with an isolated home, data/logs/run directories and loopback-only ports. Do not add Docker as an installation, CI or acceptance prerequisite.

## Tests and change discipline

1. Add a failing test before changing behavior.
2. Implement the smallest safe change.
3. Run focused tests, then the full offline suite.
4. Run memory-only compile checks and `git diff --check`.
5. Keep commits narrow and reversible.

Do not treat a pipeline completion as semantic success. Never loosen quote, time, source, case or relation validation merely to increase candidate counts.

## Data and secrets

Never commit:

```text
API keys, tokens, cookies, passwords or private keys
real corpus bodies, prompts, quotes or provider outputs
absolute paths, personal identifiers, private service addresses
```

Use placeholders in examples. Real provider and Graphiti experiments must write artifacts outside the repository and emit only sanitized summaries.

## Extending PRISM

- Providers: PRISM-owned LLM calls use the official OpenAI Python SDK through `OpenAISDKTransport` and `LLMRouter`.
- Sources: add explicit, allowlisted source integrations; do not bypass paywalls, logins, CAPTCHA or access controls.
- Prompt profiles: must be opt-in, versioned and tested against unchanged deterministic evidence/time/source/case guards.
- WebUI: remain a thin `PrismAPI → controller → NiceGUI` layer; do not access SQLite, Graphiti or an LLM router directly.

## Pull requests

Describe the user-visible behavior, test evidence, scope, and any effect on data, provider calls or network access. State clearly whether real services were exercised. Experimental behavior must be labeled as such.

## Security issues

Do not open public issues containing secrets or sensitive data. See [SECURITY.md](SECURITY.md).
