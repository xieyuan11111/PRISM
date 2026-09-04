# M2 Automatic Debate Acceptance

## Scope

M2 is the automatic multi-perspective explanation layer. It does not require
item-by-item user arbitration. Users provide a question, optional target case,
and optional historical cutoff; LLM perspectives compare the same PRISM
EvidenceBundle and produce an auditable interpretation.

## Implemented contract

- `DebateService` selects three or four observation profiles. Academic cases
  use `experimental_methods`, `mechanism_explanation`, `evidence_quality`, and
  `research_history`; policy/public-issue cases use the general profiles.
- Every selected perspective receives the same immutable, serialized evidence
  bundle for one case and cutoff. The bundle includes effective and
  invalidated timeline entries, source/evidence references, conflicts, gaps,
  and open questions.
- Independent statements are classified as `fact`, `interpretation`,
  `value_judgment`, `prediction`, or `unresolved`. Non-unresolved statements
  require existing evidence identifiers.
- Cross-examination is automatic and bounded to one round. Unknown statement
  targets and unknown evidence are rejected or dropped with an audit warning;
  no opposition is invented merely to create balance.
- Synthesis returns consensus, disagreements, sources of disagreement, key
  evidence, unresolved questions, and falsification conditions. Every returned
  point is evidence-bound. Invalid synthesis falls back to a deterministic,
  conservative result rather than adding unsupported conclusions.
- The canonical JSON contract is strict. A small adapter accepts only provider
  shapes observed in live runs (`statement`/`text`, `classifications`,
  `summary`/`finding`, `question`, `condition`, and the corresponding evidence
  aliases). Unknown or ambiguous shapes remain invalid.
- Debate runs are persisted in the project-owned SQLite `debate_audit` table
  and are idempotent by case/question/cutoff/profile/evidence-bundle hash.
  Replay returns the persisted structured result without new model calls.
- Debate interpretation is rendered separately from structured timeline facts.
  Model reasoning, ignored metadata, and failure messages are not promoted to
  facts or evidence.

## Live smoke evidence

On 2026-09-04, with the isolated PRISM-owned Neo4j/Graphiti environment and the
real configured debate provider:

- `academic-hnad-evolution`: four academic profiles available; all four
  cross-examinations completed; synthesis completed with 1 consensus, 1
  disagreement, 1 disagreement source, 1 key-evidence item, 1 unresolved
  question, and 1 falsification condition.
- `china-housing-provident-fund`: four general profiles available; all four
  cross-examinations completed; synthesis completed with 2 consensus items, 2
  disagreements, 2 disagreement sources, 3 key-evidence items, 2 unresolved
  questions, and 3 falsification conditions.

The live runs used existing PRISM corpus/ledger data and real Graphiti-backed
case timelines. The isolated Neo4j service was stopped after the runs. These
are smoke results, not a guarantee that every future provider completion will
match the same JSON shape; malformed or ambiguous completions remain safely
isolated or conservatively downgraded.

## Offline verification

The repository test suite passed after the M2 implementation and provider-shape
adapters. The live Graphiti integration tests remain opt-in and are not part of
the default offline run.

## Remaining M2 boundaries

- Provider prompt/output drift can still cause an individual perspective or
  synthesis to become unavailable; the service records the failure and keeps
  other perspectives or deterministic fallback output.
- Multi-round debate, user-directed interruption during a live round, WebUI,
  Plotly timeline views, report version UI, and broader production-scale
  pagination are later work.
- Real provider extraction quality and real case evidence quality remain
  separate from debate orchestration acceptance.
