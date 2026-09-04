# PRISM

PRISM is an open-source system for tracing how policies, academic arguments, and public issues evolve over time.

Current foundation modules:

- immutable, validated domain models;
- portable configuration with `PRISM_HOME` support;
- Markdown/PDF/OCR ingestion with raw-file retention;
- SQLite/FTS5 evidence indexing and filtered search;
- an automatic event-driven pipeline: every ingestion runs index → extract →
  accumulated-case merge → graph write, with no manual pipeline call;
- a durable per-case extraction ledger that rebuilds accumulated cases from
  local PRISM data after a restart;
- dependency-optional Graphiti/GTI temporal graph adapter and historical timeline contract;
- M1 source-backed change relations (`supersedes`, `revises`, `contradicts`,
  `triggered_by`), invalidated-fact audit views and two-cutoff comparison;
- M3 formal historical snapshots (`snapshot`/`query_historical_snapshot`)
  with a fail-closed knowledge boundary, deterministic stage filtering
  (`--stage`) and two-instant comparison (`compare`/`compare_case_history`);
- optional NiceGUI case home with a Plotly historical timeline and clickable,
  source-backed evidence-locator detail;
- opt-in Graphiti/Neo4j spike scaffolding (config, deploy template, live-test gate) that stays fully offline by default;
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
python -m prism.cli ingest input.md            # ingest + index; automatic processing is queued and finishes before exit
python -m prism.cli ingest input.md --process  # ingest + full automatic pipeline, printing its real outcome
python -m prism.cli process MATERIAL_ID        # synchronous pipeline run (or explicit idempotent replay)
python -m prism.cli merge-case CASE_ID         # rebuild and write the accumulated case from the durable ledger
python -m prism.cli cases                     # list all accumulated cases from the local ledger
python -m prism.cli add-material INPUT --case-id CASE_ID
python -m prism.cli report CASE_ID --save     # render and persist an immutable report version
python -m prism.cli report-versions CASE_ID --as-of TIMESTAMP # list/filter versions
python -m prism.cli report-version VERSION_ID # read one saved report version
python -m prism.cli rebuild-report CASE_ID    # recompute and version the report
python -m prism.cli discover MATERIAL_ID
python -m prism.cli state CASE_ID --cutoff-at 2026-09-01T00:00:00+00:00
python -m prism.cli timeline CASE_ID --as-of 2026-09-01T00:00:00+00:00
python -m prism.cli snapshot CASE_ID --as-of 2026-02-02T00:00:00+00:00 --stage publication
python -m prism.cli compare CASE_ID --earlier 2026-02-02T00:00:00+00:00 --later 2026-03-12T00:00:00+00:00
python -m prism.cli report CASE_ID --as-of 2026-09-01T00:00:00+00:00 --no-llm
```

Every `ingest` run announces the material on the event bus, so the automatic
pipeline (index → extract → accumulated-case merge → graph write) processes
it in the same runtime — there is no "index only" ingestion once the
automatic pipeline is wired. `ingest` without `--process` prints the
ingestion result while processing runs to completion before the command
exits; if that processing fails, the command exits non-zero with the
auditable failure instead of reporting success. `ingest --process` and
`process` are the synchronous entry points: they return only after the
material's pipeline run and its accumulated-case outcome exist, and a
repeated `process` on an already processed material is an explicit idempotent
replay (`"replayed": true`) that merges nothing twice.

Historical state queries apply both validity time and observation/publication
time, so later retrospective material does not leak into an earlier cutoff.
Nodes, facts and claims may carry portable evidence locators containing a
corpus-relative path, paragraph/page and source excerpt; reports render those
locations alongside `source_ids` and preserve fact/interpretation/provenance
labels.

## M1 Temporal Evolution Core

PRISM now represents a change as evidence-bearing temporal data rather than
as last-write-wins state. `TemporalFact.fact_id` is an optional stable logical
reference; a later observation with the same id can close its validity
interval without deleting the earlier graph episode. `TemporalRelation`
records `supersedes`, `revises`, `contradicts`, or `triggered_by` with separate
validity and observation time, sources, confidence, provenance and portable
evidence locators. All new fields are appended defaults or new frozen/slotted
contracts, so legacy positional construction remains valid.

`GraphService.timeline(case_id, as_of)` returns the effective state in
`entries` and known-but-no-longer-valid history in `invalidated_entries`.
Therefore an old fact is absent from the effective state after `invalid_at`
but remains traceable; its replacement is visible from its own `valid_at` and
observation boundary. Different fact ids and different source observations
are never collapsed merely because they share subject and predicate, so
contradictory facts can coexist with their individual source, evidence,
confidence and provenance records. The analyzer's `compare`, `state` and
`analyze` views expose cutoff differences, turning points, invalidated facts,
relations and unresolved questions.

Causality is intentionally narrower than chronology. A revision,
supersession or invalidation proves that a change was recorded; it does not
prove why it occurred. `AnalyzerService` emits a change reason only for an
explicit `triggered_by` relation carrying verified evidence (or for the
legacy compatibility projection of older payloads). Otherwise the M1 view
adds an `unconfirmed_change_cause` open question. Reports render revision and
conflict relations, invalidated facts and their citation locations. The
optional LLM summary is accepted only when all cited episode/source bindings
exist in the analysis; malformed or unverifiable output falls back to the
deterministic, non-causal summary.

Extraction and accumulation preserve `invalid_at`, claim `revised_by`,
explicit relations and unresolved conflicts through the pipeline and durable
case ledger into graph writes. `abstract_only`, `metadata_only`, and `blocked`
materials remain index-only and are not extracted or written. See
[`docs/m1-temporal.md`](docs/m1-temporal.md) for the offline acceptance scope
and the live-service boundary.

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
alternatives remain reportable conflict audit items and graph relations. A document publication is
counted separately from substantive evolution in deterministic reports; a
material with no supported change produces no padding publication node.

## Automatic Evolution Pipeline v0

One ingestion automatically runs the whole post-ingestion chain. When
`PrismAPI.ingest_material` (or a source fetch) publishes `material.ingested`,
the runtime's event subscriber feeds it to `PipelineService.handle_event`,
which resolves the material from the evidence store and runs index → extract
→ case merge → graph write. The subscription is registered before the event
bus starts and removed (after draining in-flight events) when the runtime
closes, so a shutdown never silently drops processing.

**Synchronous vs asynchronous semantics are explicit.** `ingest_material` is
the asynchronous, event-driven path: it returns once the material is ingested
and indexed, with automatic processing *queued* — it never claims pipeline
completion. The outcome is queryable at any moment:
`pipeline.outcome_for(MATERIAL_ID)` reports the lifecycle state
(`pending` while an attempt is in flight, `failed` after a failed attempt,
`committed` only after a successful run), `pipeline.run_for` the completed
run and `pipeline.failure_for` the last failed attempt (stage, error type,
time). `process_material(MATERIAL_ID)` waits for the in-flight or completed
run and reports the authoritative pipeline and case result: a repeated call
for an already processed material is an explicit idempotent replay
(`ProcessMaterialResult.replayed`), and a material whose last attempt failed
is retried safely — a persistent failure raises the structured
`PipelineError` (stage, material id, completed stages), never a fake success.
`process_material(PATH)` is the synchronous path: it executes the pipeline
itself, announces the material afterwards, and returns only when the
pipeline run and — when a case was produced — the accumulated-case
merge/write outcome exist. It never runs a second no-op merge after the
pipeline: the reported `case_outcome` is the outcome the pipeline's case
recorder produced, not a fresh merge.

Cross-process behavior is honest and explicit: the completed-run registry is
per-process, so a fresh process (a new CLI invocation over the same PRISM
home) re-executes the pipeline for an id whose durable state is already
committed — `replayed: false` for that genuine run. The writes stay
idempotent (graph episodes are deduplicated by episode key and the durable
ledger row is upserted under the same case), but extraction is re-run, so a
changed extraction replaces the recorded evidence; if the re-extraction now
declares a different case than the durable binding, the typed
`MaterialCaseConflict` refuses the re-binding before any write (see below).

**Subscriber failures are auditable, never fake successes.** A failure inside
the event-driven pipeline is isolated from the publisher and recorded three
ways: as a `DispatchError` on the bus (`PrismRuntime.dispatch_errors`), now
stamped with the failure time; as a per-material `PipelineFailure` via
`pipeline.failure_for(material_id)` carrying at least the material id, the
failed stage, the error type and the failure time; and as the material's
`failed` lifecycle outcome. A material whose processing failed has no
completed run and no committed outcome — retrying it is safe and a later
success clears the stale audit. A run is only recorded as completed after
every stage (including the graph/ledger write) succeeded, and the CLI exits
non-zero when automatic processing of a command's ingestion failed, so a
background failure never exits 0 as if it had succeeded.

Terminal outcomes (`failed`/`committed`) are persisted in a local
`pipeline_outcomes` table of the same SQLite file (`index.db`) — one current
row per material, hydrated into the pipeline on the next start, so a failure
from an earlier process stays queryable (there is no stale `pending` row
after a crash: `pending` is transient and lives only in the running
process). This is deliberately a local, single-process-file ledger, not a
cross-process outbox: it makes failure audits durable across restarts, but
it does not ship work between processes, and a fresh process still re-runs a
material whose durable state is committed. Durable evidence truth remains
the corpus files, `case_extraction_ledger` rows and graph episodes.

**One material binds one case.** The automatic accumulator records each
material under the single case its extraction declares; an attempt to record
an already-bound material under a different case is refused with the typed
`MaterialCaseConflict` (material id, bound cases, attempted case) before any
row or graph write, so the automatic path can never add ambiguous bindings —
within one process or across processes (a fresh process whose re-extraction
now declares another case gets the same typed refusal, with the durable
binding unchanged and the refusal auditable as a failed outcome of
`error_type: MaterialCaseConflict`).
Legacy ledgers that already contain several rows for one material stay fully
readable and reportable through `case_ids_for_material` (and
`case_for_material` raises the same typed conflict instead of an unexpected
`ValueError`).

Case writing is cumulative, never per-material: every extraction that
succeeds with a case is recorded in the durable `case_extraction_ledger`
(an additive SQLite table in the existing `index.db`), and the merged bundle
of the **whole accumulated case** is written to the graph each time — one
material never overwrites or duplicates the complete case with its own
single-material extraction. Node and claim ids are scoped per material, the
conservative `CaseBundleMerger` rules apply unchanged (unknown sources,
identifier collisions and foreign case ids raise; conflicting alternatives
are never auto-resolved), and a failed merge or graph write rolls the new
ledger entry back, so the accumulated state only ever contains materials
whose merged write succeeded. After a restart the identical accumulated
bundle is rebuilt from the local ledger alone — no LLM, no network, no
re-extraction — and rewriting the same state is fully deduplicated by
episode keys.

Evidence levels stay binding in the automatic flow: `abstract_only`,
`metadata_only` and `blocked` materials are indexed but never extracted or
written to the graph, with per-stage skip records stating the access level.
Extraction warnings, evidence gaps and unresolved conflicts never silently
disappear — they persist in the ledger verbatim and surface in every
`ProcessMaterialResult.warnings` and `CaseWriteOutcome.warnings`.

Unified entry points: `PrismAPI.process_material(MATERIAL_ID_OR_PATH)` returns
the pipeline run, the accumulated case merge/write outcome the run produced,
and the audit warnings; `PrismAPI.merge_case(CASE_ID, materials=[...])` is
the explicit reconciliation entry point, rebuilding the full accumulated
case from the durable ledger or an explicitly selected subset (idempotent,
episode-key deduplicated) — for example to repair a case whose graph state
diverged before a ledger write completed. The CLI exposes the same
operations (`ingest --process`, `process`, `merge-case`) over the identical
API surface.

PRISM is deliberately **LLM-automatic** at the candidate level: the pipeline
automatically compares evidence, resolves or preserves conflicts, binds
materials to an explicitly supplied target case when available, and records
its reasoning and uncertainty. Users add materials, ask questions, choose a
target case, or request a rebuild; they do not need to review candidates one
by one. Deterministic validation still rejects unsupported quotes, timestamps,
cross-material citations, and unsafe model output.

## M2 Automatic Debate
The CLI/API now provide automatic multi-perspective explanation over one
historical case snapshot. Academic cases use experimental-methods,
mechanism-explanation, evidence-quality, and research-history profiles;
policy/public-issue cases use the general observation profiles. Every profile
reads the same evidence bundle, produces typed and cited statements, performs
one automatic cross-examination round, and contributes to an evidence-bound
synthesis of consensus, disagreement, unresolved questions, and falsification
conditions. Debate interpretation is kept separate from structured timeline
facts. See [`docs/m2-debate.md`](docs/m2-debate.md) for the acceptance boundary
and live smoke results.

```console
python -m prism.cli debate CASE_ID \
  --question "What changed, and why do the interpretations differ?" \
  --as-of 2026-09-04T00:00:00+00:00
```

M2 has passed one real-provider smoke run for both an academic case and a
policy case. Provider output drift remains isolated or conservatively
downgraded; the default offline runtime never calls a real provider.

M3 also supports a named-perspective follow-up over an existing debate run:

```console
python -m prism.cli follow-up PARENT_RUN_ID \
  --perspective institutional_regulatory \
  --question "Why did implementation begin at this point?"
```

The follow-up reuses the parent case, historical cutoff, and evidence-bundle
hash. It is persisted separately with a parent link and is idempotent; a
changed evidence snapshot is rejected rather than silently used. This is an
API/CLI slice, not yet the NiceGUI debate theater.

Appending evidence to an active discussion can also preserve its context:

```console
python -m prism.cli add-material INPUT.md \
  --case-id CASE_ID \
  --parent-debate-run PARENT_RUN_ID
```

PRISM validates the durable parent first, processes the material through the
existing pipeline, then recomputes the GTI/analyzer evidence-bundle hash at
the parent's cutoff without calling the debate LLM. The result exposes the
prior/current hashes and whether the parent is stale; a changed snapshot is
not silently reused and no debate is re-run automatically. A successful
append creates the usual immutable `material_added` report version at that
cutoff. See `docs/m3-material-debate-link.md`.

An optional NiceGUI case home is now available for browsing accumulated cases
and loading the same GTI-backed historical snapshots used by the CLI:

```console
pip install -e ".[webui]"
python -m prism.webui
```

It binds to `127.0.0.1` with browser auto-open disabled, and remains a thin
facade client. The Plotly timeline renders each effective or invalidated entry
returned by `PrismAPI.query_historical_snapshot`; stage/kind/cutoff controls
remain facade inputs, not browser-side temporal filters. Effective entries and
invalidated facts use distinct markers and labels. Clicking a point uses its
stable `episode_key` to display the same snapshot entry and its source ids,
corpus path, paragraph/page and quote without another API read. NiceGUI and
Plotly are both lazy optional dependencies in the `webui` extra; importing the
controller does not require either one.

The debate theater, evidence upload/browser, model settings, authentication and
remote exposure are not included in this slice. See
`docs/m3-webui-case-home.md`.

## M3 Report Versioning v0

PRISM now has an API/CLI product base for multi-case operation. `cases` reads only
the project-owned durable case ledger (never Graphiti), returning case identity,
material count, evidence observation range, latest node time, update time, and
whether gaps or conflicts remain. Report versions live in the additive
`report_versions` table of the same local `index.db`: each row stores a stable
version id, `as_of`, input and Markdown hashes, summary origin, optional debate
input hash, parent version, trigger, and rendered Markdown. Rows are immutable;
an identical analysis/debate input returns the existing version without another
report LLM call. `add-material` runs the existing automatic pipeline for a known
target case and, only after extraction/merge/graph success, saves a
`material_added` report version. Extraction or cross-case binding failures create
no version. Report Markdown retains debate interpretation in its own section while
structured facts remain sourced from the analysis.

A report version can be exported to a project-relative PDF path with `report-version --pdf`:

```console
python -m prism.cli report-version rv_... --pdf reports/case-b.pdf
```

The PDF is a derived delivery artifact, not the report of record. Exports never
create or modify report versions; the same version and same bytes are idempotent, and
a different existing file is refused rather than overwritten.

Install the optional Python dependencies with `pip install -e ".[pdf]"`. PDF export
then renders Markdown with Python-Markdown, prints it with headless Microsoft Edge or
a compatible Chromium browser, and validates pages and extracted text with pypdf. Set
`PRISM_PDF_RENDERER` to an Edge/Chromium executable when autodiscovery is not
suitable. Without the dependencies or a renderer, export fails explicitly and creates
no PDF.

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

## M3 Historical Snapshot and Comparison

`snapshot` and `compare` formalize historical case queries (FR-3.6/FR-4.2/FR-4.3)
as additive facade entry points over the existing graph + analyzer stack —
no parallel fact/snapshot store is involved:

```console
python -m prism.cli snapshot CASE_ID --as-of 2026-02-02T00:00:00+00:00
python -m prism.cli snapshot CASE_ID --as-of 2026-02-02T00:00:00+00:00 --stage publication
python -m prism.cli compare CASE_ID --earlier 2026-02-02T00:00:00+00:00 --later 2026-03-12T00:00:00+00:00
```

`PrismAPI.query_historical_snapshot` (backed by `AnalyzerService.snapshot`)
returns the auditable state at one timezone-aware instant: effective nodes,
facts, claims, relations, the facts invalidated by that instant and the
case's evidence gaps, reusing `HistoricalCaseState`. The knowledge boundary
is enforced twice — `GraphService.timeline` only returns entries known by
the cutoff (`reference_time`, the observation/publication time, never later
than it), and `snapshot` fail-closes on any reader that returns an entry
known only after the cutoff, an ineffective entry, or a still-valid entry
marked invalidated. `PrismAPI.compare_case_history` delegates to the
existing `AnalyzerService.compare` and returns the existing
`EvolutionComparison` (added/removed/unchanged with both instants,
layer-classified), rejecting naive or reversed instants.

`--stage` restricts a snapshot to one deterministic recorded stage: the
fixed vocabulary (`prism.analyzer.STAGES`) is a pure lookup over markers the
graph already records — `evolution_node.node_type` stages such as
`publication`/`revision`/`expiry` (FR-4.5 policy chain and FR-4.6 discourse
chain positions) and `claim.stance` stages such as `support`/`oppose`.
Membership is never decided by an LLM, unknown stages are refused before any
graph read, filtered entries keep the layer their kind implies, and sources
and portable evidence locators are preserved. `--kind` (repeatable) applies
the existing entry-kind filter and composes with `--stage`. The older
`timeline`, `state`, `build_timeline`, `query_history` and `query_case_state`
entry points are unchanged. See
[`docs/m3-historical-snapshot.md`](docs/m3-historical-snapshot.md) for the
offline acceptance boundary.

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

## Graphiti/GTI spike (live Phase B verified)

PRISM's graph layer is a PRISM-owned Graphiti/Neo4j instance (FR-3). Phase A
implemented the code, configuration and offline tests; the Phase B live spike
passed its three opt-in integration tests on 2026-09-03 against the isolated,
loopback-only PRISM-owned Neo4j Community 5.26 server (HTTP 7475 / Bolt 7688)
with `graphiti-core==0.29.3`, the `neo4j` Python driver 6.3.0 and
`httpx==0.28.1` — the exact versions pinned in the `[graphiti]` extra below.
The live tests inject deterministic Graphiti model clients, so real
Neo4j/Graphiti persistence, search, cutoff, relation and registry behavior
were exercised WITHOUT any external LLM/embedding/rerank call (no provider
key is needed or read); real-provider extraction and reruns over real case
material remain unverified. Nothing in the default runtime imports
`graphiti-core`/`neo4j`, builds a client, probes for the optional packages or
reads Graphiti credentials. To opt in, enable the extra and the config:

```console
pip install -e ".[graphiti]"
```

```json
{
  "graphiti": {
    "enabled": true,
    "uri": "bolt://localhost:7688",
    "database": "neo4j",
    "group_id": "neo4j",
    "username_env": "",
    "password_env": "PRISM_GRAPHITI_PASSWORD",
    "timeout": 30.0
  }
}
```

The example connects to the PRISM-owned container's single built-in `neo4j`
database: the template runs Neo4j Community Edition (a single-database
edition) and sets no custom default database name. `group_id` is not a second
tenant inside the instance: graphiti-core 0.29.3 realises a Neo4j group as a
database — `add_episode` treats an explicit `group_id` as the target database
and clones the driver to `database=group_id` whenever they differ — so an
enabled config must set `database` and `group_id` to the same value (`neo4j`
here), and PRISM's config validation rejects any enabled config where they
differ. Isolation comes from the separate PRISM-owned container (its own
Neo4j home, service and data volume): two groups on one Community instance
are not supported, and the adapter's group filtering remains a defensive
contract for future multi-database/Enterprise servers. The `database` value
is adapter metadata that graphiti-core 0.29.3's
`Graphiti(uri, user, password, ...)` constructor does not consume; it must
equal `group_id` because that is the name graphiti selects as the database.
`graphiti.uri` must also carry an explicit non-default port (the standard
7474/7687 are never applied), so an enabled config cannot silently reach a
default local Neo4j.

`graphiti.uri` must not embed credentials; the config stores environment
variable *names* (`PRISM_GRAPHITI_PASSWORD`, optional `PRISM_GRAPHITI_USERNAME`),
never values. With `enabled: true` the runtime attempts the real client only
when the optional dependencies are installed; missing credentials or packages
fail with explicit errors before any service is touched. The enabled path
also creates PRISM's own SQLite-backed episode registry
(`src/prism/graph/registry.py`) and injects it into the backend: it records
each PRISM `episode_key`, the real Graphiti-assigned uuid captured from the
write, the group/database and the canonical episode body in an additive
`graphiti_episode_registry` table of the existing SQLite file (`index.db`
under the data dir — old databases migrate in place, and nothing with
credentials or absolute paths is ever stored). Duplicate writes therefore
stay no-ops across process restarts, and body-less `search` results (real
0.29.3 `EntityEdge` uuid references) are attributed through the persisted
mapping instead of in-process state alone. `PrismRuntime.close()` closes the
backend and the registry the runtime created. A caller can instead
inject `graph_backend`/`graphiti_client_factory` into `create_runtime` for
controlled integrations; a caller-injected `graph_backend` is a full
override that creates no registry.

A PRISM-owned deployment template (service `prism-graphiti-spike`, host
ports 7475/7688) and the full spike plan — side effects, acceptance
criteria, rollback, and the list of API surfaces verified by the live spike —
live in `deploy/graphiti-spike/` and `docs/graphiti-spike-plan.md`.

Live integration tests are opt-in and never part of a default CI run: they
skip unless `PRISM_GRAPHITI_URI` and `PRISM_GRAPHITI_PASSWORD` are both set in
the environment:

```console
python -m pytest tests/test_graphiti_integration.py -v
```

The Phase B spike and its full scope are recorded in
`docs/graphiti-spike-plan.md`. The three live tests verify real
Neo4j/Graphiti persistence, search, cutoff, relation and registry behavior
using deterministic injected Graphiti model clients — no external LLM or
embedding provider is called. Real-provider extraction and rerunning real
case corpus material remain separate, not-yet-verified acceptance items.
