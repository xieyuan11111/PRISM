# M1 Temporal Core Acceptance Boundary

This document states what the repository can verify offline and what the
Phase B live spike plus the 2026-09-05 real-case run confirmed. It keeps
Graphiti mechanism acceptance separate from real-provider semantic quality;
the latter remains `partial` rather than being presented as a complete
real-case semantic acceptance.

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
- A validated extraction with substantive candidates but no top-level case is
  explicitly `awaiting_case_binding`. The automatic pipeline stores its
  immutable `ExtractionResult` and source material in the project-owned
  `material_evidence_ledger` table of the existing local SQLite database; it
  does not create a `case_id` and does not submit a case-specific Graphiti
  episode. `context_only` candidates and publication-only padding do not enter
  this accumulation.
- `prism bind-material MATERIAL_ID CASE_ID` (and the matching API/CaseService
  method) is the explicit promotion boundary. It accepts only an already
  accumulated case, rechecks material/source ids, verbatim evidence and
  timestamps against the stored material, removes `missing_case_context`, and
  then uses the normal merge-and-write path. No title, tag, embedding or vector
  similarity is used to guess a case. Pending evidence remains in SQLite if
  validation, merge or graph writing fails.
- Review/synthesis results carry a separate `evidence_role` layer.
  `cited_prior_research` is secondary evidence reported by the current
  material—not an observation by that material's authors—and preserves its
  optional `cited_source_ref`; `current_synthesis` identifies the current
  authors' synthesis. Both reach the graph only after a case is present, and
  reports/CLI serialization expose the layer and citation reference.
- The optional `adjudicate` LLM role is a second automatic pass over validated
  candidates, candidate-level gaps, and conflicts. It can accept, revise,
  reject, preserve a conflict, or hold a candidate pending case binding. Every
  decision is durably versioned in the project-owned `adjudication_audit`
  table with the original candidate, revision, safe reason, deterministic
  revalidation result, and any graph episode keys. A revised candidate must
  pass the same verbatim quote, source, time, target-case, and evidence-role
  checks as first-pass extraction; a decision is never itself a graph fact.
- If the second LLM emits malformed/unsupported decision JSON or is unavailable,
  the pipeline records an `adjudication_failed` batch audit and retains the
  already validated first-pass extraction for case merge and graph write. A
  first-pass extraction failure, target-case drift, unsafe revision, or graph
  write failure remains fail-closed; no unvalidated candidate is promoted.
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

The M1 live slice was exercised on 2026-09-03 by the Phase B spike
(`tests/test_graphiti_integration.py`'s third test) against the isolated
PRISM-owned Neo4j Community 5.26 server: M1 facts with `invalid_at`,
`supersedes`/`contradicts` relation episodes, portable evidence locators,
two-cutoff exclusion and registry-restart readback all passed with
deterministic injected Graphiti model clients (no external LLM/embedding
API).  That run used synthetic fixtures, never real corpus material.

The 2026-09-05 project-external run additionally exercised real-provider
extraction over a narrow policy case and wrote the resulting accepted
records to real Graphiti. It verified the mechanism path, restart/readback,
historical cutoff and report/PDF path, while the quality gate remained
`semantic=partial` because candidate-level gaps and provider output drift
remained. The run is therefore evidence that the real production path can be
executed, not evidence that every real provider output is semantically
correct or that a complete lifecycle chain has been reconstructed.

Still unverified as a stable product guarantee are cross-case prompt
generalization, a complete proposal/publication/implementation/revision/
expiry chain, production-scale Graphiti pagination and stable relation
extraction across cases.
