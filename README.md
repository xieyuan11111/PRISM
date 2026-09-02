# PRISM

PRISM is an open-source system for tracing how policies, academic arguments, and public issues evolve over time.

Current foundation modules:

- immutable, validated domain models;
- portable configuration with `PRISM_HOME` support;
- Markdown/PDF/OCR ingestion with raw-file retention;
- SQLite/FTS5 evidence indexing and filtered search;
- dependency-optional Graphiti/GTI temporal graph adapter and historical timeline contract;
- offline tests for every completed module.

Run the offline test suite with:

```console
python -m pytest -q
```

The corpus Markdown files are the readable source of truth. SQLite is a rebuildable text index, while Graphiti/GTI is an optional temporal graph backend. Real provider credentials and external services are not required for the test suite.

## Offline CLI

Set `PRISM_HOME` to choose the local data directory. The default runtime never
opens a network client:

```console
python -m prism.cli ingest input.md
python -m prism.cli discover MATERIAL_ID
```

`discover` creates a time-bounded research plan from an indexed material. To
execute a plan, explicitly enable Firecrawl in `PRISM_HOME/config.json` and
provide the key through the configured environment variable:

```json
{
  "sources": {"whitelist": ["example.gov"]},
  "firecrawl": {
    "enabled": true,
    "api_key_env": "FIRECRAWL_API_KEY",
    "base_url": "https://api.firecrawl.dev",
    "limit": 10,
    "timeout": 10
  }
}
```

```console
# POSIX shell
export FIRECRAWL_API_KEY="..."
python -m prism.cli research MATERIAL_ID
python -m prism.cli research MATERIAL_ID --no-process
```

The key itself must not be placed in the JSON file. Firecrawl results are
only discovery leads; `research` re-fetches each public URL through PRISM's
whitelist-gated source service before ingestion.

## Concept-level research

For a long report, PRISM's research planner can extract searchable concepts
such as policy actions, indicators, mechanisms, actors, and predictions. Each
concept becomes an independently auditable query with a target of 10–20
results (10 by default, configurable up to 20). Queries carry `concept_id`, use
that result limit for Firecrawl Search, and are globally URL-deduplicated
before authoritative re-fetching. A concept may return fewer results because
of source availability, duplicate URLs, timeouts, or failed body extraction;
PRISM records those outcomes rather than padding the count with duplicates.
