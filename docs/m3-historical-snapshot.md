# M3 Historical Snapshot, Stage Filtering and Comparison — Acceptance

## Scope

This slice formalizes the historical query contract of the M3 milestone:
one GTI-backed historical snapshot per case and instant, deterministic stage
filtering, and a two-instant comparison entry point, exposed through the
shared API facade and the CLI. It reuses the existing temporal graph and
analyzer layers — GraphService timelines, the `[valid_at, invalid_at)`
contract, observation/publication boundaries, `AnalyzerService.compare` and
`HistoricalCaseState` — and adds no parallel fact/snapshot database and no
LLM involvement. WebUI (NiceGUI), debate-time stage rebuilds and a full
real-provider production Graphiti acceptance are **not** part of this slice.

## Implemented contract

### Historical snapshot (`AnalyzerService.snapshot`, facade
`query_historical_snapshot`, CLI `snapshot`)

A snapshot of one case at one timezone-aware instant returns, in one frozen
`HistoricalCaseState`:

- effective `evolution_node` stages (the nodes bucket),
- effective `temporal_fact` entries (facts),
- effective `claim` entries (interpretations — claims are the only
  interpretation-layer kind),
- effective `temporal_relation` entries (relations),
- facts whose `invalid_at` is at or before the instant (invalidated facts),
- the case's evidence gaps at that instant,
- case type and status known at that instant.

The projection reads the graph service's timeline for `as_of` only; nothing
is persisted or cached, so there is no second source of truth beside GTI.
The knowledge boundary is enforced twice: `GraphService.timeline` only
returns entries known by `as_of` (`reference_time <= as_of`, where
`reference_time` is the observation/publication time, bounded by bound
materials), and `snapshot` fail-closes on any reader that violates the
contract — an entry known only after `as_of`, an entry listed as effective
outside its window, or a still-valid entry listed as invalidated raises
`ValueError` naming the episode. A future material or publication therefore
never leaks into an earlier state (FR-3.6, FR-4.2, U2).

### Deterministic stage filtering

A stage is a caller-selectable slice of the recorded evolution. Membership
is a pure lookup over markers the graph already records — never decided by
an LLM and never invented:

| Stage family | Marker | Entry kind |
|---|---|---|
| Node stages (policy chain: publication, implementation, revision, reversal, replacement, expiry; discourse chain: proposal, draft, response, debate, consensus, open_question; plus interpretation nodes) | `node_type` | `evolution_node` |
| Stance stages (support, oppose, conditional, uncertain claim directions) | `stance` | `claim` |

The fixed vocabulary is exported as `prism.analyzer.STAGES`; any other value
raises `ValueError` naming every legal stage before a graph read. Entries
keep the layer their kind implies (node stages are fact-layer, stance stages
are interpretation-layer), so a filter can never move an interpretation into
a fact view; sources and portable evidence locators are preserved verbatim.
Facts, relations and provenance entries carry no stage marker and never
match a stage — they remain visible in unfiltered snapshots. Evidence gaps
describe the case at the cutoff (computed on the unfiltered projection), so
a narrow stage never manufactures an `empty_timeline` gap for a case that
has evidence outside the stage. The existing entry-kind filter (`kinds`,
CLI `--kind`) composes with the stage filter.

### Two-instant comparison (facade `compare_case_history`, CLI `compare`)

`compare_case_history(case_id, earlier, later, kinds=...)` delegates to the
existing `AnalyzerService.compare` and returns the existing
`EvolutionComparison` (added/removed/unchanged with the two instants).
Both instants must be timezone-aware and `earlier <= later`; reversed or
naive instants are refused before any graph read. Every change keeps its
fact/interpretation layer, so "the facts changed" stays separable from "the
interpretations changed" (FR-4.3, FR-4.4). The CLI validates the stage and
kind vocabulary at parse time and renders the same stable JSON as the other
commands.

### Compatibility

`build_timeline`, `query_history` and `query_case_state` keep their
behavior and names; `snapshot`/`compare` are additive facade entry points
over the same injected analyzer. No existing test or caller needs to change.

## Offline fixture coverage

`tests/test_m3_snapshot.py` runs fully offline against synthetic backends
and injected fakes:

- snapshot projection of every bucket with layer separation and verbatim
  source/evidence preservation;
- invalid `stage`/`kinds` and naive instants are refused before any graph
  read; stage-without-members returns empty buckets without a fabricated
  gap;
- fail-closed knowledge boundary: readers leaking future reference time,
  ineffective entries or misplaced invalidated entries raise;
- full-stack GTI-shaped scenario through `GraphService` over an in-memory
  backend: an old fact superseded at a later cutoff lands in
  `invalidated_facts` with its original sources and evidence, nodes and
  claims recorded only by a later-published material never leak into the
  earlier cutoff, and `compare` reports the expected added/removed/unchanged
  sets;
- registry-based restart readback: a fresh backend over the same fake store
  and durable registry rebuilds identical snapshots and comparisons;
- facade delegation (stage/kinds forwarding, missing-analyzer and
  missing-operation errors, backward-compatible `query_case_state`);
- CLI parsing, delegation and JSON output for `snapshot` and `compare`,
  including usage errors that never call the API.

## Boundaries

- The slice is verified offline with synthetic episodes. The underlying
  episode round-trip (write, search, registry restart readback) was
  spike-verified against the isolated PRISM-owned Graphiti/Neo4j
  environment with deterministic clients for earlier milestones; this slice
  adds no new live-server claim.
- Stage semantics are query views over recorded markers. Recognizing chain
  positions the model does not record (for example discourse "divergence")
  remains out of scope; no stage is synthesized.
- "User specifies a stage" is implemented at the API/CLI query level.
  Specifying or rebuilding a stage *during a live debate*, the NiceGUI
  timeline view and production-scale Graphiti acceptance remain later work.
