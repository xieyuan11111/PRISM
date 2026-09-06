# Security Policy

## Reporting a vulnerability

Do not publish API keys, tokens, private corpus content, prompts, personal data or exploit details in a public issue.

Until a dedicated security contact is published, report a suspected vulnerability privately to the repository owner through the repository hosting platform. Include a minimal reproduction and impact description, but redact secrets and private data.

## Security boundaries

- PRISM's WebUI is loopback-only by default.
- Docker and Docker Compose are not part of the project route.
- Real LLM credentials belong in local protected environment variables or secret stores, never in Git, corpus, graph, report or logs.
- Real provider/Graphiti experiments must keep materials and detailed outputs outside the repository.
- Evidence, time, source, case and relation validation are fail-closed boundaries; reports must not turn `partial` into `pass`.

## Supported configuration

The current v1 development branch is the supported configuration. Optional extras are installed only when needed; core offline functionality should not require network access.