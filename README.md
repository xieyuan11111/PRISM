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
python -m prism.cli state CASE_ID --cutoff-at 2026-09-01T00:00:00+00:00
```

Historical state queries apply both validity time and observation/publication
time, so later retrospective material does not leak into an earlier cutoff.
Nodes, facts and claims may carry portable evidence locators containing a
corpus-relative path, paragraph/page and source excerpt; reports render those
locations alongside `source_ids` and preserve fact/interpretation/provenance
labels.

## Evolution Extraction v0

`ExtractionService.extract_material(material, corpus_path=...)` is the public,
evidence-bound extraction entry point used by the pipeline. It sends the
normalized Markdown body through the configured LLM Router's `extract` role,
strictly validates the JSON response, and then verifies every candidate quote
against the corpus text before producing graph-ready domain objects. A failed
locator is retained as an explicit extraction evidence gap; it is never
fabricated or silently written to the graph. `PrismAPI.extract_material(...)`
exposes the same operation when an extraction service is configured.

The schema keeps event occurrence (`happened_at`), effective validity
(`valid_at`), and observation/publication (`observed_at`) separate. Forecasts
remain uncertain claims rather than confirmed temporal facts, and contradictory
alternatives remain reportable conflict audit items. A document publication is
counted separately from substantive evolution in deterministic reports; a
material with no supported change produces no padding publication node.

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

## Scholarly evidence levels

PRISM does not restrict research to one discipline. Academic candidates can be
resolved through public DOI metadata services such as Crossref and OpenAlex,
with domain-specific indexes added as optional adapters. Evidence is labeled
explicitly:

- `fulltext`: the public article body was collected;
- `abstract_only`: a public abstract was recovered, but not the article body;
- `metadata_only`: title/author/venue/DOI metadata was recovered without an
  abstract; the corpus record is a bibliographic placeholder, not full text;
- `blocked`: access was refused, a paywall/login/captcha was encountered, or
  the public response could not be validated.

An abstract is stored as `summary` and never promoted to `content` as if it
were full text. The bibliographic identity of a resolved work — `authors`,
`container_title` (venue), and `doi` — is preserved end to end: it travels on
the source item, lands in the corpus frontmatter, and is indexed so indexed
records and search hits keep the same labels and DOI. When a DOI is present
and the article page is blocked, the API can fall back to Crossref/OpenAlex
metadata without credentials (bare public GETs only; OpenAlex is consulted at
most once per DOI resolution). The result remains clearly labeled and
traceable.

When Crossref answers with metadata but no abstract, the resolver makes one
OpenAlex enrichment request. Enrichment merges, it never replaces: Crossref
stays authoritative for `title`, `doi`, `authors`, `container_title`, `link`
and `published_at`, and OpenAlex contributes only the abstract plus whatever
fields Crossref lacks — for example authors, a publication date, or a venue
taken from OpenAlex's `primary_location.source.display_name` when Crossref has
no `container_title`. An OpenAlex record without an abstract adds nothing, so
the Crossref metadata-only record is kept exactly as it was; a failed
enrichment does the same. In every path OpenAlex is requested at most once per
DOI resolution.

`blocked` page responses are detected conservatively and the boundary is part
of the safety design: a response is treated as an access-verification wall
(CAPTCHA, "checking your browser", ...) only when wall phrasing appears
within the first 400 characters of visible text **and** the whole visible text
is under 2000 characters. Long pages that merely discuss or quote such
phrasing — a CAPTCHA usability survey, an outage report mentioning "Access
denied" — are never blocked; only short, top-heavy interstitials are.

## Concept-level research

For a long report, PRISM's research planner can extract searchable concepts
such as policy actions, indicators, mechanisms, actors, and predictions. Each
concept becomes an independently auditable query with a target of 10–20
results (10 by default, configurable up to 20). Queries carry `concept_id`, use
that result limit for Firecrawl Search, and are globally URL-deduplicated
before authoritative re-fetching. A concept may return fewer results because
of source availability, duplicate URLs, timeouts, or failed body extraction;
PRISM records those outcomes rather than padding the count with duplicates.
