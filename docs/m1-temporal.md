# M1 Temporal Core Acceptance Boundary

This document states what the repository can verify offline. It does not
claim that a real Graphiti or Neo4j deployment has passed live acceptance.

## Implemented semantics

- `TemporalFact` retains `[valid_at, invalid_at)` validity, observation time,
  source ids, confidence, provenance and evidence location. Its optional
  `fact_id` lets later immutable observations refine the same logical fact.
- `TemporalRelation` is frozen, slotted and timezone-aware. It records
  `supersedes`, `revises`, `contradicts`, and `triggered_by` with its own time,
  sources, confidence, provenance and evidence.
- Timeline reads separate effective `entries` from `invalidated_entries`.
  Invalidated facts remain auditable and are never deleted merely because
  they are no longer effective.
- Logical version coalescing applies only when a stable logical id is present.
  Unrelated observations and conflicting facts retain separate episodes and
  do not overwrite one another by insertion order.
- Claim `revised_by`, fact `invalid_at`, explicit relations and extraction
  conflicts survive the extraction → ledger/merge → graph path. Exact replay
  remains episode-key idempotent, including after reconstruction with the
  durable registry path.
- Analyzer output distinguishes fact/interpretation/provenance layers,
  supports historical cutoff state and two-cutoff comparison, and exposes
  turning points, invalidated facts, relations, open questions and evidence
  gaps.
- Only an evidence-bearing `triggered_by` relation becomes an M1 change
  reason. Temporal ordering, an invalidation timestamp, a revision node, a
  `revises` relation, or a `supersedes` relation alone is not treated as a
  cause; the cause is reported as unconfirmed.
- Reports and the shared API/CLI serialization include revision/conflict
  relations, invalidated facts, source ids and portable evidence locations.
  Publication nodes are counted separately from substantive evolution.
- Model-distilled summaries must cite episode/source pairs present in the
  analyzed evidence. Invalid output is rejected and deterministically
  downgraded without adding an unsupported conclusion.

## Offline fixture coverage

`tests/test_m1_temporal.py` covers two cutoffs around a revision, an old fact
that becomes ineffective but remains traceable, a visible replacement,
coexisting contradictory facts with independent attribution, explicit and
missing trigger evidence, claim revision projection, strict relation
extraction, API/CLI agreement, repeated-write idempotency and legacy
constructor compatibility. The broader graph, analyzer, extraction, case,
pipeline, report, API and CLI suites remain regression coverage.

All tests use synthetic fixtures and injected memory/local backends. They do
not collect or process a real policy/news case and do not start an external
database or model provider.

## Live Graphiti boundary

The Graphiti adapter and durable episode registry are implemented, but this
M1 slice does not start or probe a real Graphiti/Neo4j service. Until the
opt-in live suite is run against a dedicated PRISM-owned instance, database
write/search behavior, server-side temporal edge materialization and restart
behavior remain live-validation items. The offline timeline semantics are
PRISM service-layer guarantees; they must not be described as proof of an
external Graphiti deployment.
